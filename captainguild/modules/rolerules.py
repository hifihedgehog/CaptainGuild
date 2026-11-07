"""Role rules: roles that follow from the roles a member holds.

A rule has up to ten conditions, all of which must hold, and roles to add and to remove when they do. A rule that keeps in sync
also takes its added roles back from a member who stops matching, unless another rule that matches them adds the same role.

For one member, the rules run on a copy of their roles, in order, again and again until nothing changes. Then the bot makes one
edit for the whole difference. Rules still changing things after ten passes are undoing each other, so that member keeps their roles,
the rules involved are switched off with a note, and staff hear about it.

Changes go through one queue per server, at about one edit a second, so a full pass over a large server never spends the server's
role rate limit at once. A member whose roles just changed goes ahead of a full pass. The queue is worked out again from the members
themselves after a restart, the same way a sync started by hand picks up where it stopped.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..core import access
from ..core.durations import humanize
from ..core.members import whole_member_list
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import answer_place, role_is_safe
from ..core.validate import Field, Invalid, Schema, ValidationError, boolean, integer, optional, snowflake, snowflakes, text, whole_number
from ..web.api import ApiError
from .reactionroles import Turns
from .roles import Confirm, find_roles, missing_roles_sentence, send_pages

log = logging.getLogger(__name__)

TYPES = {
    "has_some": "has any of", "has_all": "has all of", "lacks_some": "is missing one of", "lacks_all": "has none of",
    "exactly": "has exactly {n} of", "at_least": "has at least {n} of", "at_most": "has at most {n} of",
    "more_than": "has more than {n} of", "fewer_than": "has fewer than {n} of",
}
COUNT_TYPES = frozenset({"exactly", "at_least", "at_most", "more_than", "fewer_than"})
MAX_RULES = 50
MAX_CONDITIONS = 10
MAX_ROLES = 25
MAX_COUNT = 250
MAX_PASSES = 10
# Role changes share one rate limit per server, about one a second.
SPACING = 1.0
# How many servers change roles at once, and how many turns one server can hold.
PARALLEL_SERVERS = 8
PER_SERVER = 1
# A save that changes more members than this asks the person to confirm the count.
CONFIRM_OVER = 200
SYNC_SECONDS = 600
PREVIEW_NAMES = 20
# How long the bot waits for the gateway to show its own edit of a member, which arrives a moment after the edit.
RECENT_SECONDS = 10.0
JOB_POST_SECONDS = 15
JOB_SAVE_EVERY = 25
OUTAGE_PAUSE = 300.0
TRANSIENT = (aiohttp.ClientError, OSError, asyncio.TimeoutError)
NOTICE_TITLE = "Role rules need a look"
PAUSED_TITLE = "Role rules paused"
LOOP_NOTE = "These rules undo each other's changes: {names}. The bot switched them off. Change one so they agree, then switch them back on."
GONE_NOTE = "Every role this rule adds or removes was deleted, so the bot switched it off."
CONDITION_GONE_NOTE = "A role this rule checks for was deleted, so the bot switched it off. Pick the roles it checks now, then switch it back on."
CHANGED_MEANWHILE = "The role rules changed while you decided, so nothing was saved. Run the command again."
PAGE_OUT_OF_DATE = "Someone changed the role rules since this page loaded, so nothing was saved. Reload the page to see their change."
# How much of a pause note the rules' names can take. The note holds 300 characters.
NOTE_NAMES_ROOM = 150
EMBED_COLOR = 0x00A2F8
NOT_READY = "The bot is still loading this server's member list after a restart, so it can't tell who that changes yet. Try again in a few minutes."
OLD_SHAPE = "{name} isn't the kind of rule these commands can change. Change it on the dashboard's Role rules page."


# ---- checking what's saved ----

def _condition(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise Invalid(f"{where}: each condition needs a type and roles.")
    kind = value.get("type")
    if kind not in TYPES:
        raise Invalid(f"{where}: pick a type for each condition.")
    try:
        roles = snowflakes(MAX_ROLES)(value.get("roles") if value.get("roles") is not None else [])
    except Invalid as error:
        raise Invalid(f"{where}: {error}") from error
    if not roles:
        raise Invalid(f"{where}: each condition needs at least one role.")
    count = 0
    if kind in COUNT_TYPES:
        try:
            count = integer(0, MAX_COUNT)(value.get("count", 0))
        except Invalid as error:
            raise Invalid(f"{where}: the number needs to be a whole number from 0 to {MAX_COUNT}.") from error
    return {"type": kind, "roles": roles, "count": count}


def rule_list(value: Any) -> list[dict]:
    """The rules in the order they run. A rule sent without an ID gets the next free one."""
    if not isinstance(value, list):
        raise Invalid("Must be a list.")
    if len(value) > MAX_RULES:
        raise Invalid(f"A server can have at most {MAX_RULES} role rules.")
    given = [whole_number(row.get("id"), 2 ** 31) if isinstance(row, dict) else None for row in value]
    next_id = max([number for number in given if number] or [0]) + 1
    rows: list[dict] = []
    names: set[str] = set()
    ids: set[int] = set()
    for number, row in enumerate(value, 1):
        if not isinstance(row, dict):
            raise Invalid(f"Rule {number} needs a name, conditions and roles.")
        try:
            name = text(100, minimum=1)(row.get("name"))
        except Invalid as error:
            raise Invalid(f"Rule {number} needs a name of up to 100 characters.") from error
        if name.casefold() in names:
            raise Invalid(f"Two rules are called {name}. Each rule needs its own name.")
        names.add(name.casefold())
        rule_id = given[number - 1]
        if not rule_id or rule_id in ids:
            rule_id, next_id = next_id, next_id + 1
        ids.add(rule_id)
        conditions = row.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise Invalid(f"{name}: a rule needs at least one condition.")
        if len(conditions) > MAX_CONDITIONS:
            raise Invalid(f"{name}: a rule can have at most {MAX_CONDITIONS} conditions.")
        clean_conditions = [_condition(item, name) for item in conditions]
        try:
            add = snowflakes(MAX_ROLES)(row.get("add") if row.get("add") is not None else [])
            remove = snowflakes(MAX_ROLES)(row.get("remove") if row.get("remove") is not None else [])
            enabled = boolean(row.get("enabled", True))
            keep = boolean(row.get("keep_in_sync", False))
        except Invalid as error:
            raise Invalid(f"{name}: {error}") from error
        if not add and not remove:
            raise Invalid(f"{name}: a rule needs a role to add or remove.")
        if set(add) & set(remove):
            raise Invalid(f"{name}: a rule can't both add and remove the same role.")
        paused = "" if enabled else text(300)(row.get("paused") or "")
        rows.append({"id": rule_id, "name": name, "enabled": enabled, "paused": paused, "conditions": clean_conditions, "add": add, "remove": remove, "keep_in_sync": keep})
    return rows


# ---- the rules themselves ----

@dataclass(frozen=True)
class Rule:
    id: int
    name: str
    conditions: tuple[tuple[str, frozenset, int], ...]
    add: frozenset
    remove: frozenset
    keep: bool


def compile_rules(rows: list[dict]) -> list[Rule]:
    """The switched-on rules, in the order they run."""
    return [Rule(row["id"], row["name"], tuple((c["type"], frozenset(c["roles"]), c["count"]) for c in row["conditions"]), frozenset(row["add"]), frozenset(row["remove"]), row["keep_in_sync"])
            for row in rows if row["enabled"]]


def condition_holds(kind: str, roles: frozenset, count: int, held: set[int]) -> bool:
    found = len(roles & held)
    if kind == "has_some":
        return found > 0
    if kind == "has_all":
        return found == len(roles)
    if kind == "lacks_some":
        return found < len(roles)
    if kind == "lacks_all":
        return found == 0
    if kind == "exactly":
        return found == count
    if kind == "at_least":
        return found >= count
    if kind == "at_most":
        return found <= count
    if kind == "more_than":
        return found > count
    if kind == "fewer_than":
        return found < count
    raise ValueError(kind)


def rule_holds(rule: Rule, held: set[int]) -> bool:
    return all(condition_holds(kind, roles, count, held) for kind, roles, count in rule.conditions)


@dataclass
class Outcome:
    final: set[int]
    why: dict[int, tuple[bool, str]] = field(default_factory=dict)  # role ID: (added, the rule's name)
    used: list[str] = field(default_factory=list)
    settled: bool = True
    looping: set[int] = field(default_factory=set)


def settle(rules: list[Rule], start: set[int], actionable: set[int]) -> Outcome:
    """Run the rules on a copy of these roles until nothing changes. Only roles the bot can change are changed."""
    held = set(start)
    why: dict[int, tuple[bool, str]] = {}
    used: list[str] = []
    for _ in range(MAX_PASSES):
        changed: set[int] = set()
        for rule in rules:
            if rule_holds(rule, held):
                gain = (rule.add & actionable) - held
                lose = rule.remove & actionable & held
                held |= gain
                held -= lose
                for role_id in gain:
                    why[role_id] = (True, rule.name)
                for role_id in lose:
                    why[role_id] = (False, rule.name)
                if gain or lose:
                    changed.add(rule.id)
                    if rule.name not in used:
                        used.append(rule.name)
            elif rule.keep:
                drop = {role_id for role_id in rule.add & actionable & held
                        if not any(other.id != rule.id and role_id in other.add and rule_holds(other, held) for other in rules)}
                if drop:
                    held -= drop
                    for role_id in drop:
                        why[role_id] = (False, rule.name)
                    changed.add(rule.id)
                    if rule.name not in used:
                        used.append(rule.name)
        if not changed:
            final_why = {role_id: reason for role_id, reason in why.items() if (role_id in held) != (role_id in start)}
            return Outcome(held, final_why, used)
    return Outcome(set(start), settled=False, looping=changed)


def held_ids(member: Any, guild_id: int) -> set[int]:
    """The roles a member holds, by ID, with @everyone, which every member holds and the gateway leaves out."""
    raw = getattr(member, "_roles", None)
    ids = set(raw) if raw is not None else {role.id for role in member.roles}
    ids.add(guild_id)
    return ids


def terms_key(rule: dict) -> str:
    """Changes to these make the rule hand its roles to different people, so the dashboard checks its roles again."""
    blob = json.dumps([rule["conditions"], rule["keep_in_sync"]], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def old_shape(rule: dict) -> dict | None:
    """The rule as a role set: a target role, tracked roles, and an ignored and a required role. None for a rule that can't be read that way."""
    if not rule["keep_in_sync"] or len(rule["add"]) != 1 or rule["remove"]:
        return None
    found: dict[str, Any] = {"target": rule["add"][0], "tracked": None, "ignored": None, "required": None}
    for condition in rule["conditions"]:
        kind, roles = condition["type"], condition["roles"]
        if kind == "has_some" and found["tracked"] is None:
            found["tracked"] = list(roles)
        elif kind == "lacks_all" and found["ignored"] is None and len(roles) == 1:
            found["ignored"] = roles[0]
        elif kind == "has_all" and found["required"] is None and len(roles) == 1:
            found["required"] = roles[0]
        else:
            return None
    return found if found["tracked"] else None


def from_old(name: str, target: int, tracked: list[int], ignored: int | None, required: int | None, *, rule_id: int | None = None, enabled: bool = True, paused: str = "") -> dict:
    """A role set as a rule that keeps its target in sync: has any tracked role, has none of the ignored role, and has the required role."""
    conditions = [{"type": "has_some", "roles": list(tracked), "count": 0}]
    if ignored:
        conditions.append({"type": "lacks_all", "roles": [ignored], "count": 0})
    if required:
        conditions.append({"type": "has_all", "roles": [required], "count": 0})
    row = {"name": name, "enabled": enabled, "paused": paused, "conditions": conditions, "add": [target], "remove": [], "keep_in_sync": True}
    if rule_id is not None:
        row["id"] = rule_id
    return row


def shared_name(guild: Any, roles: list, typed: str) -> str | None:
    """Why a role named in this text can't be used: several roles have its name and it wasn't given by mention or ID, so it may be the wrong one."""
    for role in roles:
        if str(role.id) in typed:
            continue
        folded = role.name.casefold()
        if sum(1 for other in guild.roles if other.name.casefold() == folded) > 1:
            return f"Several roles are called {discord.utils.escape_markdown(role.name)[:100]}. Name that one by mention or ID."
    return None


def loop_note(names: list[str]) -> str:
    """The note on rules that undo each other, naming as many as fit and then how many more."""
    shown: list[str] = []
    for name in names:
        if shown and len(", ".join([*shown, name])) > NOTE_NAMES_ROOM:
            break
        shown.append(name)
    rest = len(names) - len(shown)
    return LOOP_NOTE.format(names=", ".join(shown) + (f" and {rest} more" if rest else ""))


def rules_version(rules: list[dict]) -> str:
    """Names the rules as saved, so a page holding an older copy can be told so."""
    return hashlib.sha256(json.dumps(rules, sort_keys=True, default=str).encode()).hexdigest()[:16]


def role_text(guild: Any, role_id: int) -> str:
    return f"<@&{role_id}>" if guild is None or guild.get_role(role_id) is not None else "a deleted role"


def describe(guild: Any, rule: dict) -> str:
    parts = []
    for condition in rule["conditions"]:
        words = TYPES[condition["type"]].format(n=condition["count"])
        parts.append(f"{words} {', '.join(role_text(guild, role_id) for role_id in condition['roles'])}")
    lines = [f"When a member {' and '.join(parts)}:"]
    if rule["add"]:
        lines.append(f"adds {', '.join(role_text(guild, role_id) for role_id in rule['add'])}")
    if rule["remove"]:
        lines.append(f"removes {', '.join(role_text(guild, role_id) for role_id in rule['remove'])}")
    if rule["keep_in_sync"]:
        lines.append("and takes the added roles back when they stop matching")
    state = "on" if rule["enabled"] else ("paused: " + discord.utils.escape_markdown(rule["paused"]) if rule["paused"] else "off")
    return f"**{discord.utils.escape_markdown(rule['name'])}** ({state})\n" + " ".join(lines) + "."


# ---- the queue ----

@dataclass
class SyncJob:
    guild_id: int
    user_id: int | None
    channel_id: int | None
    message_id: int | None
    total: int = 0
    done: int = 0
    changed: int = 0
    started_at: float = 0.0


@dataclass
class Queue:
    live: dict[int, None] = field(default_factory=dict)
    bulk: dict[int, None] = field(default_factory=dict)
    task: asyncio.Task | None = None
    job: SyncJob | None = None


class Again(Exception):
    """Discord or the network failed in a way that passes. The member goes back in the queue."""


class CreateFlags(commands.FlagConverter, case_insensitive=True):
    tracked: str = commands.flag(positional=True, description="Roles that give the target, by mention, ID or name")
    ignored: Optional[discord.Role] = commands.flag(default=None, description="A role that keeps someone from getting the target")
    required: Optional[discord.Role] = commands.flag(default=None, description="A role someone must also hold to get the target")


class RoleRules(Module):
    key = "rolerules"
    category = "Roles"
    schema = Schema({
        "rules": Field([], rule_list),
        "log_channel": Field(None, optional(snowflake)),
    })
    migrations = [
        """
        CREATE TABLE role_rule_jobs (
            guild_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            channel_id INTEGER,
            message_id INTEGER,
            total INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0,
            changed INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL,
            saved_at REAL
        );
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._queues: dict[int, Queue] = {}
        self._passes: dict[int, asyncio.Task] = {}
        self._again: set[int] = set()
        self._turns = Turns(PARALLEL_SERVERS, PER_SERVER)
        self._recent: dict[tuple[int, int], tuple[float, frozenset]] = {}
        self._told: set[tuple[int, str]] = set()
        self._loop: asyncio.Task | None = None
        self._starting: set[int] = set()

    async def on_load(self) -> None:
        self.bot.settings.listen(self.key, self._settings_changed)

    async def cog_unload(self) -> None:
        tasks = [task for task in [self._loop, *self._passes.values(), *(queue.task for queue in self._queues.values())] if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- what the dashboard checks ----

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        """A switched-on rule's roles to add and to remove, under keys that change with its conditions, so pointing a rule at different people counts as handing its roles out again."""
        given: dict[str, set[int]] = {}
        for rule in settings["rules"]:
            if rule["enabled"]:
                key = f"rules:{rule['id']}:{terms_key(rule)}"
                given[f"{key}:add"] = set(rule["add"])  # apart, so moving a role from one list to the other counts as new
                given[f"{key}:remove"] = set(rule["remove"])
        return given

    def role_refusal(self, guild: Any, role: Any) -> str | None:
        if role.is_default():
            return "Everyone always has @everyone, so a rule can't add or remove it."
        if role.managed:
            return f"{role.name} belongs to an integration, and Discord doesn't let anyone hand it out."
        if not role_is_safe([name for name, on in role.permissions if on], self.bot.settings.get(guild.id, COMMANDS_MODULE)):
            return f"{role.name} holds permissions the bot won't hand out while elevated roles are off on the Commands page."
        return None

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {"log_channel": {settings["log_channel"]}} if settings["log_channel"] else {}

    def transient_roles(self, guild_id: int) -> set[int]:
        """A role a rule keeps in sync comes and goes with the rule, so it isn't put back on someone who returns."""
        return {role_id for rule in self.cfg(guild_id)["rules"] if rule["enabled"] and rule["keep_in_sync"] for role_id in rule["add"]}

    def reach_refusal(self, guild: Any, member: Any, before: dict, after: dict) -> str | None:
        """The dashboard's checks for a command: the same roles refused for the same reasons, in the same words."""
        had = self.roles_given(before)
        admin = access.is_admin(member)
        for key, roles in self.roles_given(after).items():
            for role_id in sorted(roles - had.get(key, set())):
                role = guild.get_role(role_id)
                if role is None:
                    continue
                if role >= guild.me.top_role:
                    return f"{role.name} sits at or above the bot's highest role, so the bot can't hand it out."
                refusal = self.role_refusal(guild, role)
                if refusal:
                    return refusal
                if not admin and role >= member.top_role:
                    return f"{role.name} sits at or above your highest role, so you can't set it up to be handed out."
        channel_id = after["log_channel"]
        if channel_id and channel_id != before["log_channel"]:
            channel = guild.get_channel_or_thread(channel_id)
            if channel is not None:
                return access.post_refusal(member, channel)
        return None

    # ---- who changes ----

    def _rules(self, guild_id: int) -> list[Rule]:
        return compile_rules(self.cfg(guild_id)["rules"])

    def _notice(self, guild: Any, topic: str, text_: str, *, title: str = NOTICE_TITLE) -> None:
        """Staff hear about a problem once, and again only after it was fixed and came back."""
        if (guild.id, topic) in self._told:
            return
        self._told.add((guild.id, topic))
        log.warning("%s in %s: %s", title, guild.id, text_)
        logs = self.bot.module("logging")
        if logs is not None:
            logs.staff_notice(guild, title, text_)

    def _actionable(self, guild: Any, rules: list[Rule], *, tell: bool = True) -> set[int]:
        """The rules' roles the bot can change now. A role it can't is left alone and the rule stays as it is."""
        me = guild.me
        if me is None:
            return set()
        if not me.guild_permissions.manage_roles:
            if tell:
                self._notice(guild, "manage_roles", "The bot needs the Manage Roles permission to run role rules.")
            return set()
        self._told.discard((guild.id, "manage_roles"))
        commands_settings = self.bot.settings.get(guild.id, COMMANDS_MODULE)
        found: set[int] = set()
        for role_id in {role_id for rule in rules for role_id in rule.add | rule.remove}:
            role = guild.get_role(role_id)
            if role is None or role.is_default() or role.managed:
                continue
            if role >= me.top_role:
                if tell:
                    self._notice(guild, f"above:{role_id}", f"{role.mention} sits at or above the bot's highest role, so role rules skip it. Move the bot's role above it.")
                continue
            if not role_is_safe([name for name, on in role.permissions if on], commands_settings):
                if tell:
                    self._notice(guild, f"power:{role_id}", f"{role.mention} holds permissions the bot won't hand out while elevated roles are off on the Commands page, so role rules skip it.")
                continue
            self._told.discard((guild.id, f"above:{role_id}"))
            self._told.discard((guild.id, f"power:{role_id}"))
            found.add(role_id)
        return found

    def _members(self, guild: Any) -> list[Any]:
        """Everyone the rules apply to: not the bot, and not someone still on the rules screen, whose roles Discord won't change."""
        me = guild.me
        return [member for member in guild.members if not member.pending and (me is None or member.id != me.id)]

    async def _outcomes(self, guild: Any, rules: list[Rule], actionable: set[int]) -> Any:
        """Each member and what the rules make of them, yielding now and then so a large server doesn't hold up everything else."""
        for index, member in enumerate(self._members(guild)):
            start = held_ids(member, guild.id)
            yield member, start, settle(rules, start, actionable)
            if index % 500 == 499:  # 50 rules over 500 members take about 25 ms
                await asyncio.sleep(0)

    async def needing(self, guild: Any, paused: list[str] | None = None) -> list[int]:
        """Who the rules would change now, worked out in memory. Rules that undo each other are switched off first, and their note goes in `paused`."""
        await self._pause_emptied(guild)
        rules = self._rules(guild.id)
        if not rules:
            return []
        actionable = self._actionable(guild, rules)
        out: list[int] = []
        looping: set[int] = set()
        async for member, start, outcome in self._outcomes(guild, rules, actionable):
            if not outcome.settled:
                looping |= outcome.looping
            elif outcome.final != start:
                out.append(member.id)
        if looping:
            try:
                note = await self._pause_looping(guild, looping)
            except Exception:  # the members the other rules change still go ahead
                log.exception("Couldn't switch off role rules in %s that undo each other", guild.id)
                return out
            if paused is not None and note:
                paused.append(note)
            return []  # the save that switched them off starts a fresh pass
        return out

    async def preview(self, guild: Any, rows: list[dict]) -> dict:
        """What saving these rules would change: how many members, and who gains and loses each role."""
        rules = compile_rules(rows)
        report: dict[str, Any] = {"members": 0, "roles": [], "looping": []}
        if guild is None or not rules:
            return report
        actionable = self._actionable(guild, rules, tell=False)
        roles: dict[int, dict] = {}
        looping: set[int] = set()
        async for member, start, outcome in self._outcomes(guild, rules, actionable):
            if not outcome.settled:
                looping |= outcome.looping
                continue
            if outcome.final == start:
                continue
            report["members"] += 1
            for role_id in outcome.final ^ start:
                entry = roles.setdefault(role_id, {"role_id": role_id, "gain": 0, "lose": 0, "gain_names": [], "lose_names": []})
                side = "gain" if role_id in outcome.final else "lose"
                entry[side] += 1
                if len(entry[f"{side}_names"]) < PREVIEW_NAMES:
                    entry[f"{side}_names"].append(member.display_name)
        report["roles"] = sorted(roles.values(), key=lambda entry: -(entry["gain"] + entry["lose"]))
        report["looping"] = [rule.name for rule in rules if rule.id in looping]
        return report

    # ---- switching rules off ----

    async def _pause(self, guild: Any, rule_ids: set[int], note: str) -> list[str]:
        rows = copy.deepcopy(self.cfg(guild.id)["rules"])
        names = []
        for row in rows:
            if row["id"] in rule_ids and row["enabled"]:
                row["enabled"] = False
                row["paused"] = note
                names.append(row["name"])
        if names:
            await self.bot.settings.save(guild.id, self.key, {"rules": rows}, action="rolerules.pause")
        return names

    async def _pause_looping(self, guild: Any, rule_ids: set[int]) -> str | None:
        names = [row["name"] for row in self.cfg(guild.id)["rules"] if row["id"] in rule_ids]
        note = loop_note(names)
        paused = await self._pause(guild, rule_ids, note)
        if paused:
            self._told.discard((guild.id, "loop"))
            self._notice(guild, "loop", note, title=PAUSED_TITLE)
            return note
        return None

    async def _pause_emptied(self, guild: Any) -> None:
        """A rule whose roles to add and remove were all deleted has nothing left to do. A rule that checks for a deleted role means something else now, since
        nobody holds that role: a required role gone would take the rule's roles from everyone. A person decides what it should check."""
        rows = [row for row in self.cfg(guild.id)["rules"] if row["enabled"]]
        emptied = {row["id"] for row in rows if all(guild.get_role(role_id) is None for role_id in [*row["add"], *row["remove"]])}
        blind = {row["id"] for row in rows if row["id"] not in emptied and any(guild.get_role(role_id) is None for condition in row["conditions"] for role_id in condition["roles"])}
        for found, note in ((emptied, GONE_NOTE), (blind, CONDITION_GONE_NOTE)):
            names = await self._pause(guild, found, note) if found else []
            if names:
                self._notice(guild, f"gone:{','.join(sorted(names))}", f"{', '.join(names)}: {note}", title=PAUSED_TITLE)

    # ---- the queue ----

    def _queue(self, guild_id: int) -> Queue:
        queue = self._queues.get(guild_id)
        if queue is None:
            queue = self._queues[guild_id] = Queue()
        return queue

    def _start_worker(self, guild_id: int) -> None:
        queue = self._queue(guild_id)
        if queue.task is None or queue.task.done():
            queue.task = asyncio.create_task(self._work(guild_id, queue), name=f"rolerules-work-{guild_id}")

    def enqueue(self, guild_id: int, user_ids: Any, *, live: bool = False) -> None:
        queue = self._queue(guild_id)
        place = queue.live if live else queue.bulk
        for user_id in user_ids:
            place[user_id] = None
        if place:
            self._start_worker(guild_id)

    async def _work(self, guild_id: int, queue: Queue) -> None:
        failures = 0
        last_post = time.monotonic()
        try:
            while True:
                while queue.live or queue.bulk:
                    from_bulk = not queue.live
                    place = queue.bulk if from_bulk else queue.live
                    user_id = next(iter(place))
                    del place[user_id]
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        queue.live.clear()
                        queue.bulk.clear()
                        break
                    edited = False
                    await self._turns.acquire(guild_id)
                    try:
                        edited = await self.apply(guild, user_id)
                        failures = 0
                    except Again as error:
                        failures += 1
                        (queue.bulk if from_bulk else queue.live)[user_id] = None  # to the back of its own line, so one member's trouble doesn't hold the rest
                        log.warning("Role rules in %s are waiting out a Discord problem: %s", guild_id, error)
                        from_bulk = False
                    finally:
                        self._turns.release(guild_id)
                    job = queue.job
                    if job is not None and from_bulk:
                        job.done += 1
                        job.changed += 1 if edited else 0
                        if job.done % JOB_SAVE_EVERY == 0:
                            await self.db.run("UPDATE role_rule_jobs SET done = ?, changed = ?, total = ?, saved_at = ? WHERE guild_id = ?", (job.done, job.changed, max(job.total, job.done), time.time(), guild_id))
                        if time.monotonic() - last_post >= JOB_POST_SECONDS:
                            last_post = time.monotonic()
                            await self._update_post(guild, job)
                    if failures:
                        await asyncio.sleep(min(OUTAGE_PAUSE, SPACING * 2 ** min(failures, 10)))
                    elif edited:
                        await asyncio.sleep(SPACING)
                if queue.job is None:
                    break
                await self._finish_job(guild_id, queue)  # then a look at the queue again, since a change can arrive during this
        except asyncio.CancelledError:
            raise  # the bot is stopping. The job's row stays, so it picks up at the next start
        except Exception:
            log.exception("Role rules stopped working through the queue of %s", guild_id)

    async def apply(self, guild: Any, user_id: int) -> bool:
        """Bring one member in line with the rules in one edit. Returns whether it changed anything."""
        member = guild.get_member(user_id)
        if member is None or member.pending or (guild.me is not None and member.id == guild.me.id):
            return False
        start = held_ids(member, guild.id)
        recent = self._recent.get((guild.id, user_id))
        if recent is not None and time.monotonic() - recent[0] < RECENT_SECONDS and frozenset(start) == recent[1]:
            return False  # the cache still shows the roles from before the bot's own edit. The update the gateway sends for it comes back through here
        if not self._rules(guild.id):
            return False
        await self._pause_emptied(guild)
        rules = self._rules(guild.id)
        if not rules:
            return False
        actionable = self._actionable(guild, rules)
        outcome = settle(rules, start, actionable)
        if not outcome.settled:
            try:
                await self._pause_looping(guild, outcome.looping)
            except Exception:  # the queue goes on to the next member
                log.exception("Couldn't switch off role rules in %s that undo each other", guild.id)
            return False
        if outcome.final == start:
            return False
        reason = f"Role rules: {', '.join(outcome.used)}"[:500]
        try:
            if len(outcome.final ^ start) == 1:
                # One role is one PUT or DELETE, which leaves every other role alone.
                role = guild.get_role(next(iter(outcome.final ^ start)))
                if role is None:
                    return False
                await (member.add_roles if role.id in outcome.final else member.remove_roles)(role, reason=reason)
            else:
                # An edit sends the whole list, so it starts from Discord's copy. The gateway's can miss a role another feature gave a moment ago.
                fresh = await guild.fetch_member(user_id)
                fresh_start = held_ids(fresh, guild.id)
                if fresh_start != start:
                    outcome = settle(rules, fresh_start, actionable)
                    if not outcome.settled or outcome.final == fresh_start:
                        return False
                    start = fresh_start
                removed = start - outcome.final
                roles = [role for role in fresh.roles if not role.is_default() and role.id not in removed]
                roles += [role for role in (guild.get_role(role_id) for role_id in sorted(outcome.final - start)) if role is not None]
                await fresh.edit(roles=roles, reason=reason)
        except discord.NotFound:
            return False  # they left
        except discord.Forbidden:
            self._notice(guild, "forbidden", "Discord refused a role change from role rules. Check that the bot has Manage Roles and that its role sits above the roles the rules change.")
            return False
        except discord.HTTPException as error:
            if error.status >= 500 or error.status == 429:
                raise Again(str(error)) from error
            log.warning("A role rules change in %s for %s failed: %s", guild.id, user_id, error)
            return False
        except TRANSIENT as error:
            raise Again(str(error)) from error
        self._told.discard((guild.id, "forbidden"))
        self._remember(guild.id, user_id, start, outcome.final)
        self._log_changes(guild, member, outcome)
        return True

    def _remember(self, guild_id: int, user_id: int, before: set[int], after: set[int]) -> None:
        """The roles a member had before the bot's edit, so a cache that still shows them isn't read as a reason to edit again, and after it, so the gateway's
        copy of the edit ends the wait. A change after that is a person's, even one that puts the old roles back."""
        now = time.monotonic()
        if len(self._recent) > 5000:
            self._recent = {key: value for key, value in self._recent.items() if now - value[0] < RECENT_SECONDS}
        self._recent[(guild_id, user_id)] = (now, frozenset(before), frozenset(after))

    def _log_changes(self, guild: Any, member: Any, outcome: Outcome) -> None:
        channel_id = self.cfg(guild.id)["log_channel"]
        if not channel_id:
            return
        lines = []
        for role_id, (added, rule_name) in sorted(outcome.why.items(), key=lambda item: (not item[1][0], item[0])):
            verb, joiner = ("Added", "to") if added else ("Removed", "from")
            lines.append(f"{verb} <@&{role_id}> {joiner} {member.mention} ({discord.utils.escape_markdown(rule_name)}).")
        if lines:
            self.bot.logs.post(channel_id, discord.Embed(description="\n".join(lines)[:4000], color=EMBED_COLOR))

    # ---- full passes ----

    def schedule_pass(self, guild_id: int) -> None:
        """Look at every member soon. A pass already running looks again once it's done."""
        running = self._passes.get(guild_id)
        if running is not None and not running.done():
            self._again.add(guild_id)
            return
        self._passes[guild_id] = asyncio.create_task(self._pass(guild_id), name=f"rolerules-pass-{guild_id}")

    async def _pass(self, guild_id: int) -> None:
        try:
            while True:
                self._again.discard(guild_id)
                guild = self.bot.get_guild(guild_id)
                if guild is None or not self.cfg(guild_id)["rules"]:
                    return
                if not await whole_member_list(guild):
                    return  # the next background pass tries again, and nothing changes from a partial list
                self.enqueue(guild_id, await self.needing(guild))
                if guild_id not in self._again:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("A role rules pass over %s failed", guild_id)

    async def _settings_changed(self, guild_id: int, settings: dict) -> None:
        self.schedule_pass(guild_id)

    async def _background(self) -> None:
        while True:
            await asyncio.sleep(SYNC_SECONDS)
            for guild_id in self.bot.settings.guilds_with(self.key):
                if self._rules(guild_id):
                    self.schedule_pass(guild_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._loop is None or self._loop.done():
            self._loop = asyncio.create_task(self._background(), name="rolerules-background")
        await self.resume()

    async def resume(self) -> None:
        """At a start, pick up a sync someone started and look at everyone once, since roles changed while the bot was away."""
        for row in await self.db.all("SELECT * FROM role_rule_jobs"):
            try:
                await self._resume_job(row)
            except Exception:  # one server's bad row must not stop another server's sync
                log.exception("Could not pick up the role rules sync of %s", row["guild_id"])
        for guild_id in self.bot.settings.guilds_with(self.key):
            if self._rules(guild_id) and guild_id not in {row["guild_id"] for row in await self.db.all("SELECT guild_id FROM role_rule_jobs")}:
                self.schedule_pass(guild_id)

    async def _resume_job(self, row: dict) -> None:
        guild_id = row["guild_id"]
        guild = self.bot.get_guild(guild_id)
        queue = self._queue(guild_id)
        if guild is None or queue.job is not None:
            return
        job = SyncJob(guild_id, row["user_id"], row["channel_id"], row["message_id"], total=row["total"], done=row["done"], changed=row["changed"], started_at=row["started_at"])
        queue.job = job
        if not await whole_member_list(guild):
            queue.job = None
            self.schedule_pass(guild_id)
            return
        targets = await self.needing(guild)
        job.total = job.done + len(targets)
        self.enqueue(guild_id, targets)
        if not targets:
            await self._finish_job(guild_id, queue)

    # ---- syncs started by a person ----

    async def start_sync(self, guild: Any, user_id: int | None, channel: Any = None) -> SyncJob | str:
        """Look at every member now, with a progress post. Returns the job, or why it didn't start."""
        queue = self._queue(guild.id)
        if queue.job is not None or guild.id in self._starting:
            return "A sync is already running in this server."
        if not self._rules(guild.id):
            return "No role rules are switched on."
        job = SyncJob(guild.id, user_id, None, None, started_at=time.time())
        # Marked before the first await, so a second start stops at the check above. The job goes on the queue only with its members,
        # so a worker emptying the queue meanwhile can't finish it before it starts.
        self._starting.add(guild.id)
        try:
            if not await whole_member_list(guild):
                return NOT_READY
            paused: list[str] = []
            targets = await self.needing(guild, paused)
            if paused:
                return paused[0]
            job.total = len(targets)
            if channel is not None:
                try:
                    sent = await channel.send(embed=self._progress_embed(guild, job), allowed_mentions=discord.AllowedMentions.none())
                    job.channel_id, job.message_id = channel.id, sent.id
                except discord.HTTPException as error:
                    log.info("Couldn't post role rules progress in %s: %s", guild.id, error)
            await self.db.run(
                "INSERT OR REPLACE INTO role_rule_jobs (guild_id, user_id, channel_id, message_id, total, done, changed, started_at, saved_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)",
                (guild.id, user_id, job.channel_id, job.message_id, job.total, job.started_at, job.started_at),
            )
        finally:
            self._starting.discard(guild.id)
        queue.job = job
        self.enqueue(guild.id, targets)
        if not targets:
            await self._finish_job(guild.id, queue)
        return job

    def job_json(self, guild_id: int) -> dict | None:
        queue = self._queues.get(guild_id)
        job = queue.job if queue else None
        if job is None:
            return None
        return {"running": True, "done": job.done, "total": max(job.total, job.done), "changed": job.changed}

    def _progress_embed(self, guild: Any, job: SyncJob, *, ending: str | None = None) -> discord.Embed:
        total = max(job.total, job.done)
        filled = round((job.done / total if total else 1) * 20)
        lines = [f"`{'#' * filled}{'.' * (20 - filled)}` {job.done:,} of {total:,} members", f"{job.changed:,} changed."]
        lines.append(ending or f"About {humanize(max(0, total - job.done) * SPACING, parts=2)} left at most.")
        return discord.Embed(title="Syncing role rules", description="\n".join(lines), color=0x53DDAC if ending else EMBED_COLOR)

    async def _update_post(self, guild: Any, job: SyncJob, *, ending: str | None = None) -> None:
        if not job.message_id or guild is None:
            return
        channel = guild.get_channel_or_thread(job.channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(job.message_id).edit(embed=self._progress_embed(guild, job, ending=ending), allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            job.message_id = None
        except (discord.HTTPException, *TRANSIENT) as error:
            log.warning("Couldn't update a role rules progress post in %s: %s", guild.id, error)

    async def _finish_job(self, guild_id: int, queue: Queue) -> None:
        job = queue.job
        queue.job = None
        await self.db.run("DELETE FROM role_rule_jobs WHERE guild_id = ?", (guild_id,))
        if job is not None:
            await self._update_post(self.bot.get_guild(guild_id), job, ending="Finished.")

    async def on_guild_restored(self, guild_id: int) -> None:
        """A backup's rules replace this server's. A sync from back then isn't wanted now, and everyone is looked at again under the restored rules."""
        queue = self._queues.get(guild_id)
        if queue is not None:
            queue.job = None
            queue.bulk.clear()
        await self.db.run("DELETE FROM role_rule_jobs WHERE guild_id = ?", (guild_id,))
        self.schedule_pass(guild_id)

    # ---- events ----

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        recent = self._recent.get((after.guild.id, after.id))
        if recent is not None and recent[2] in (frozenset(held_ids(before, after.guild.id)), frozenset(held_ids(after, after.guild.id))):
            del self._recent[(after.guild.id, after.id)]  # the gateway has shown the bot's edit
        if after.pending or not self._rules(after.guild.id):
            return
        if before.pending or held_ids(before, after.guild.id) != held_ids(after, after.guild.id):
            self.enqueue(after.guild.id, [after.id], live=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not member.pending and self._rules(member.guild.id):
            self.enqueue(member.guild.id, [member.id], live=True)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._rules(role.guild.id):
            await self._pause_emptied(role.guild)

    # ---- saving from a command ----

    def _named(self, guild_id: int, name: str) -> tuple[list[dict], int]:
        rows = copy.deepcopy(self.cfg(guild_id)["rules"])
        folded = name.strip().casefold()
        index = next((index for index, row in enumerate(rows) if row["name"].casefold() == folded), -1)
        return rows, index

    async def _find(self, ctx: commands.Context, name: str) -> tuple[list[dict], int] | None:
        rows, index = self._named(ctx.guild.id, name)
        if index < 0:
            await ctx.fail(f"There's no role rule called {discord.utils.escape_markdown(name[:100])}. `/rolerule list` shows them.")
            return None
        return rows, index

    async def _save(self, ctx: commands.Context, rows: list[dict], action: str, *, settings: dict | None = None) -> int | None:
        """Every check the dashboard runs, then the count to confirm when it's large. Returns how many members the save changes, or None when it didn't save.
        `rows` was made from the rules as they stand now, and a save that finds them changed after the wait refuses rather than put back what someone else replaced."""
        guild = ctx.guild
        before = self.cfg(guild.id)
        form = settings or {"rules": rows}
        try:
            after = self.schema.clean(form, base=before)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values())))
            return None
        refusal = self.reach_refusal(guild, ctx.author, before, after)
        if refusal:
            await ctx.fail(refusal)
            return None
        changed = 0
        if after["rules"] != before["rules"]:
            await ctx.defer(ephemeral=True)  # a large server takes a moment to look over
            if not await whole_member_list(guild):
                await ctx.fail(NOT_READY)
                return None
            report = await self.preview(guild, after["rules"])
            changed = report["members"]
            if report["members"] > CONFIRM_OVER:
                view = Confirm(ctx.author.id, yes="Yes, save it")
                sent = await ctx.send(f"Saving this changes the roles of {report['members']:,} members. Save it?", view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                if sent is None and ctx.interaction is None:
                    return None
                await view.wait()
                if not view.answer:
                    await ctx.fail("Nothing was saved.")
                    return None
            if self.cfg(guild.id)["rules"] != before["rules"]:
                await ctx.fail(CHANGED_MEANWHILE)
                return None
        await self.bot.settings.save(guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        return changed

    async def _change_old(self, ctx: commands.Context, name: str, action: str, change: Any) -> None:
        """An edit in the old role set terms. A rule those terms can't describe is left for the dashboard."""
        found = await self._find(ctx, name)
        if found is None:
            return
        rows, index = found
        row = rows[index]
        shape = old_shape(row)
        if shape is None:
            await ctx.fail(OLD_SHAPE.format(name=discord.utils.escape_markdown(row["name"])))
            return
        problem = change(shape)
        if problem:
            await ctx.fail(problem)
            return
        rows[index] = from_old(row["name"], shape["target"], shape["tracked"], shape["ignored"], shape["required"], rule_id=row["id"], enabled=row["enabled"], paused=row["paused"])
        if await self._save(ctx, rows, action) is not None:
            await ctx.ok(f"Saved {discord.utils.escape_markdown(row['name'])}.")

    def _roles_from(self, ctx: commands.Context, text_: str) -> tuple[list, str | None]:
        try:
            found, missing = find_roles(ctx.guild, text_ or "")
        except Invalid as error:
            return [], str(error)
        if missing:
            return [], missing_roles_sentence(missing)
        if not found:
            return [], "Name at least one role."
        return found, shared_name(ctx.guild, found, text_ or "")

    def _typed_refusal(self, ctx: commands.Context, roles: list) -> str | None:
        """A typed role is read by name too. A slash command's role picker can't be ambiguous."""
        if ctx.interaction is not None:
            return None
        return shared_name(ctx.guild, [role for role in roles if role is not None], ctx.message.content or "")

    # ---- commands ----

    @commands.hybrid_group(name="rolerule", aliases=["rolerules"], fallback="list", description="Roles that follow from the roles members hold")
    @commands.guild_only()
    @requires("manage_roles")
    async def rolerule(self, ctx: commands.Context) -> None:
        rows = self.cfg(ctx.guild.id)["rules"]
        await send_pages(ctx, "Role rules", [f"{number}. {describe(ctx.guild, row)}" for number, row in enumerate(rows, 1)], empty="No role rules yet. Make one with `/rolerule create` or on the dashboard.")

    @rolerule.command(name="show", description="Show one role rule")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name")
    async def rolerule_show(self, ctx: commands.Context, *, name: str) -> None:
        found = await self._find(ctx, name)
        if found is not None:
            rows, index = found
            await send_pages(ctx, "Role rule", [describe(ctx.guild, rows[index])])

    @rolerule.command(name="create", description="Make a rule that keeps a target role on members with any tracked role")
    @requires("manage_roles")
    @app_commands.describe(name="A name for the rule", target="The role members get while they match")
    async def rolerule_create(self, ctx: commands.Context, name: str, target: discord.Role, *, flags: CreateFlags) -> None:
        tracked, problem = self._roles_from(ctx, flags.tracked)
        problem = problem or self._typed_refusal(ctx, [target, flags.ignored, flags.required])
        if problem:
            await ctx.fail(problem)
            return
        rows = copy.deepcopy(self.cfg(ctx.guild.id)["rules"])
        rows.append(from_old(name, target.id, [role.id for role in tracked], flags.ignored.id if flags.ignored else None, flags.required.id if flags.required else None))
        if await self._save(ctx, rows, "rolerules.create") is not None:
            await ctx.ok(f"Made {discord.utils.escape_markdown(name.strip()[:100])}. Members with any of {', '.join(role.mention for role in tracked)} get {target.mention}, and lose it when they stop matching.")

    @rolerule.command(name="delete", description="Delete a role rule")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name")
    async def rolerule_delete(self, ctx: commands.Context, *, name: str) -> None:
        found = await self._find(ctx, name)
        if found is None:
            return
        rows, index = found
        gone = rows.pop(index)
        changed = await self._save(ctx, rows, "rolerules.delete")
        if changed is None:
            return
        name = discord.utils.escape_markdown(gone["name"])
        if changed:
            # Another rule that keeps the same role in sync no longer counts this one, so it takes the role from whoever matched only this one.
            await ctx.ok(f"Deleted {name}. That changes the roles of {changed:,} members, about one a second.")
        else:
            await ctx.ok(f"Deleted {name}. Roles it gave stay where they are.")

    async def _switch(self, ctx: commands.Context, name: str, on: bool) -> None:
        found = await self._find(ctx, name)
        if found is None:
            return
        rows, index = found
        rows[index]["enabled"] = on
        if await self._save(ctx, rows, "rolerules.enable" if on else "rolerules.disable") is not None:
            await ctx.ok(f"{discord.utils.escape_markdown(rows[index]['name'])} is {'on' if on else 'off'}.")

    @rolerule.command(name="enable", description="Switch a role rule on")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name")
    async def rolerule_enable(self, ctx: commands.Context, *, name: str) -> None:
        await self._switch(ctx, name, True)

    @rolerule.command(name="disable", description="Switch a role rule off. Roles it gave stay where they are.")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name")
    async def rolerule_disable(self, ctx: commands.Context, *, name: str) -> None:
        await self._switch(ctx, name, False)

    @rolerule.command(name="sync", description="Check every member against the role rules now, with progress")
    @requires("manage_roles")
    async def rolerule_sync(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        result = await self.start_sync(ctx.guild, ctx.author.id, self._post_channel(ctx))
        if isinstance(result, str):
            await ctx.fail(result)
            return
        if result.total == 0:
            await ctx.ok("Every member already matches the rules.")
            return
        await ctx.ok(f"Started. {result.total:,} members need a change, about one a second.")

    def _post_channel(self, ctx: commands.Context) -> Any:
        """Where the progress post goes, following the server's mute and redirect choices. A slash reply can only be edited for 15 minutes, so it's a plain message."""
        decision = getattr(ctx, "decision", None)
        if decision is None:
            return ctx.channel
        if decision.muted:
            return None
        target = decision.redirect or decision.restricted_channel
        if target and target != "dm":
            return answer_place(ctx.guild, target) or ctx.channel
        return ctx.channel

    @rolerule.command(name="test", description="Check whether a member matches a rule, and how many match now")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name", member="The member to check. Leave it out to check yourself.")
    async def rolerule_test(self, ctx: commands.Context, name: str, member: Optional[discord.Member] = None) -> None:
        await ctx.defer(ephemeral=True)  # the member list can take a while to load after a restart
        found = await self._find(ctx, name)
        if found is None:
            return
        rows, index = found
        row = rows[index]
        rule = compile_rules([{**row, "enabled": True}])[0]
        member = member or ctx.author
        held = held_ids(member, ctx.guild.id)
        lines = []
        for condition, (kind, roles, count) in zip(row["conditions"], rule.conditions):
            mark = "yes" if condition_holds(kind, roles, count, held) else "no"
            lines.append(f"{mark}: {TYPES[kind].format(n=count)} {', '.join(role_text(ctx.guild, role_id) for role_id in condition['roles'])}")
        verdict = "matches" if rule_holds(rule, held) else "doesn't match"
        lines.insert(0, f"{member.mention} {verdict} {discord.utils.escape_markdown(row['name'])}.")
        if await whole_member_list(ctx.guild):
            matching = sum(1 for other in self._members(ctx.guild) if rule_holds(rule, held_ids(other, ctx.guild.id)))
            lines.append(f"{matching:,} members match it now.")
        else:
            lines.append("The member list is still loading, so the count of members who match comes later.")
        await send_pages(ctx, "Role rule test", lines)

    @rolerule.command(name="logchannel", description="Post each role rule change in a channel, or stop")
    @requires("manage_roles")
    @app_commands.describe(channel="The channel. Leave it out to stop posting.")
    async def rolerule_logchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        if await self._save(ctx, [], "rolerules.logchannel", settings={"log_channel": channel.id if channel else None}) is not None:
            await ctx.ok(f"Role rule changes now post in {channel.mention}." if channel else "Role rule changes no longer post anywhere.")

    @rolerule.command(name="add_tracked", description="Add tracked roles to a rule made with create")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name", roles="Roles by mention, ID or name")
    async def rolerule_add_tracked(self, ctx: commands.Context, name: str, *, roles: str) -> None:
        found, problem = self._roles_from(ctx, roles)

        def change(shape: dict) -> str | None:
            if problem:
                return problem
            shape["tracked"] += [role.id for role in found if role.id not in shape["tracked"]]
            return None
        await self._change_old(ctx, name, "rolerules.add_tracked", change)

    @rolerule.command(name="remove_tracked", description="Remove tracked roles from a rule made with create")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name", roles="Roles by mention, ID or name")
    async def rolerule_remove_tracked(self, ctx: commands.Context, name: str, *, roles: str) -> None:
        found, problem = self._roles_from(ctx, roles)

        def change(shape: dict) -> str | None:
            if problem:
                return problem
            left = [role_id for role_id in shape["tracked"] if role_id not in {role.id for role in found}]
            if not left:
                return "A rule needs at least one tracked role. Delete the rule instead."
            shape["tracked"] = left
            return None
        await self._change_old(ctx, name, "rolerules.remove_tracked", change)

    @rolerule.command(name="change_target", description="Change the role a rule made with create gives")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name", target="The new target role")
    async def rolerule_change_target(self, ctx: commands.Context, name: str, target: discord.Role) -> None:
        await self._change_old(ctx, name, "rolerules.change_target", lambda shape: self._typed_refusal(ctx, [target]) or shape.update(target=target.id))

    @rolerule.command(name="set_ignored", description="Set the role that keeps someone from a rule's target")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name", role="The ignored role")
    async def rolerule_set_ignored(self, ctx: commands.Context, name: str, role: discord.Role) -> None:
        await self._change_old(ctx, name, "rolerules.set_ignored", lambda shape: self._typed_refusal(ctx, [role]) or shape.update(ignored=role.id))

    @rolerule.command(name="clear_ignored", description="Remove a rule's ignored role")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name")
    async def rolerule_clear_ignored(self, ctx: commands.Context, *, name: str) -> None:
        await self._change_old(ctx, name, "rolerules.clear_ignored", lambda shape: shape.update(ignored=None))

    @rolerule.command(name="set_required", description="Set a role someone must also hold to get a rule's target")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name", role="The required role")
    async def rolerule_set_required(self, ctx: commands.Context, name: str, role: discord.Role) -> None:
        await self._change_old(ctx, name, "rolerules.set_required", lambda shape: self._typed_refusal(ctx, [role]) or shape.update(required=role.id))

    @rolerule.command(name="clear_required", description="Remove a rule's required role")
    @requires("manage_roles")
    @app_commands.describe(name="The rule's name")
    async def rolerule_clear_required(self, ctx: commands.Context, *, name: str) -> None:
        await self._change_old(ctx, name, "rolerules.clear_required", lambda shape: shape.update(required=None))

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/rolerules", self._api_rules)
        router.post("/rolerules/preview", self._api_preview)
        router.put("/rolerules", self._api_save)
        router.get("/rolerules/sync", self._api_job)
        router.post("/rolerules/sync", self._api_sync)

    def _checked_rules(self, form: Any) -> list[dict]:
        try:
            return rule_list(form.get("rules"))
        except Invalid as error:
            raise ValidationError({"rules": str(error)}) from error

    async def _report(self, req: Any, rows: list[dict]) -> dict:
        guild = self.bot.get_guild(req.guild_id)
        if guild is not None and not await whole_member_list(guild):
            raise ApiError(503, NOT_READY)
        return await self.preview(guild, rows)

    def _page_copy(self, guild_id: int) -> dict:
        return {**self.schema.masked(self.cfg(guild_id)), "version": rules_version(self.cfg(guild_id)["rules"])}

    async def _api_rules(self, req: Any) -> dict:
        return self._page_copy(req.guild_id)

    def _check_version(self, req: Any, form: dict) -> None:
        """A change to the rules names the version it was made from, which `GET /rolerules` gives. The whole list is saved, so one made from an older copy would put back what was replaced since."""
        if "rules" in form and form.get("version") != rules_version(self.cfg(req.guild_id)["rules"]):
            raise ApiError(409, PAGE_OUT_OF_DATE)

    async def _api_preview(self, req: Any) -> dict:
        form = await req.json()
        return await self._report(req, self._checked_rules(form))

    async def _api_save(self, req: Any) -> dict:
        """The page's Save. A change to more than CONFIRM_OVER members comes back asking for the count, and goes through once the page sends it."""
        form = await req.json()
        settings: dict[str, Any] = {}
        self._check_version(req, form)
        if "rules" in form:
            rows = self._checked_rules(form)
            settings["rules"] = form["rules"]
            if rows != self.cfg(req.guild_id)["rules"]:
                report = await self._report(req, rows)
                if report["members"] > CONFIRM_OVER and whole_number(form.get("confirm")) != report["members"]:
                    raise ApiError(409, f"Saving this changes the roles of {report['members']:,} members.", {"confirm": str(report["members"])})
                self._check_version(req, form)  # the preview can wait on the member list
        if "log_channel" in form:
            settings["log_channel"] = form["log_channel"]
        await req.save_settings(self.key, settings, action="rolerules.update")
        return self._page_copy(req.guild_id)

    async def _api_job(self, req: Any) -> dict:
        return {"job": self.job_json(req.guild_id)}

    async def _api_sync(self, req: Any) -> dict:
        guild = self.bot.get_guild(req.guild_id)
        if guild is None:
            return {"job": None}
        result = await self.start_sync(guild, req.user.id)
        if isinstance(result, str):
            raise ApiError(409, result)
        await req.record("rolerules.sync", detail=f"{result.total:,} members to change")
        return {"job": self.job_json(req.guild_id)}
