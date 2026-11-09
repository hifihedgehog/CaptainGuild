"""Which invite brought someone in, and whether a join through it counts.

Discord doesn't say which invite a member used. The bot keeps each invite's use count and reads them all again when someone joins: the one
whose count went up is theirs. A one-use invite is deleted as it's used, so when no count went up, an invite one use short of its limit
that disappeared is the answer. When two invites went up at once, as in a burst of joins, the join stays unattributed rather than guessed.

A join that doesn't count is "fake" for one or more reasons. Some are fixed when the person joins, and others follow them: a new member
stops being new once they've been in the server long enough, and gaining a required role clears that reason for good.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import store

# Each reason's key, as settings and the dashboard name it, and its bit.
FAKE_REASONS = {
    "self": 0x1,          # used their own invite
    "rejoin": 0x2,        # had been a member before
    "young_account": 0x4,  # account younger than the server's minimum when they joined
    "new_member": 0x8,    # hasn't been in the server for the server's minimum yet
    "no_avatar": 0x10,    # had no profile picture when they joined
    "missing_role": 0x20,  # hasn't held the required roles yet
    "onboarding": 0x40,   # hasn't finished onboarding
    "home_actions": 0x80,  # hasn't finished the server guide's first steps
    "pending": 0x100,     # hasn't passed membership screening
    "left": 0x200,        # has left
    "bot": 0x400,         # is a bot
    "temporary": 0x800,   # joined through a temporary-membership invite
}
VANITY_PREFIX = "vanity:"


@dataclass(frozen=True)
class InviteState:
    uses: int
    max_uses: int | None
    inviter_id: int | None
    temporary: bool


def attribute(before: dict[str, InviteState] | None, after: dict[str, InviteState] | None) -> str | None:
    """The code someone joined through, from invite use counts read before and after the join, or None when it can't be told."""
    if before is None or after is None:
        return None
    risen = [code for code, state in after.items() if state.uses > (before[code].uses if code in before else 0)]
    if len(risen) == 1:
        return risen[0]
    if risen:
        return None
    used_up = [code for code, state in before.items() if code not in after and state.max_uses and state.uses + 1 >= state.max_uses]
    return used_up[0] if len(used_up) == 1 else None


def join_facts(*, inviter_id: int | None, member_id: int, rejoined: bool, has_avatar: bool, bot: bool, temporary: bool) -> int:
    facts = 0
    if inviter_id is not None and inviter_id == member_id:
        facts |= store.JOIN_SELF
    if rejoined:
        facts |= store.JOIN_REJOIN
    if not has_avatar:
        facts |= store.JOIN_NO_AVATAR
    if bot:
        facts |= store.JOIN_BOT
    if temporary:
        facts |= store.JOIN_TEMPORARY
    return facts


def reasons(row: dict, settings: dict, now: float, member: Any | None) -> int:
    """The fake reasons that apply to a join row now. `member` is the person's current member object, or None if they're gone."""
    bits = 0
    facts = row.get("facts") or 0
    for fact, reason in ((store.JOIN_SELF, "self"), (store.JOIN_REJOIN, "rejoin"), (store.JOIN_NO_AVATAR, "no_avatar"), (store.JOIN_BOT, "bot"),
                         (store.JOIN_TEMPORARY, "temporary")):
        if facts & fact:
            bits |= FAKE_REASONS[reason]
    minimum_account = settings.get("invite_min_account_minutes") or 0
    if minimum_account and row.get("account_created") is not None and row["at"] - row["account_created"] < minimum_account * 60:
        bits |= FAKE_REASONS["young_account"]
    if row.get("left_at") is not None or member is None:
        bits |= FAKE_REASONS["left"]
        return bits
    minimum_join = settings.get("invite_min_join_minutes") or 0
    if minimum_join and now - row["at"] < minimum_join * 60:
        bits |= FAKE_REASONS["new_member"]
    required = set(settings.get("invite_required_roles") or [])
    if required and row.get("role_met_at") is None and not holds_required(member, required, settings.get("invite_required_match", "any")):
        bits |= FAKE_REASONS["missing_role"]
    flags = getattr(member, "flags", None)
    if flags is not None:
        if not getattr(flags, "completed_onboarding", True) and getattr(flags, "started_onboarding", False):
            bits |= FAKE_REASONS["onboarding"]
        if getattr(flags, "started_home_actions", False) and not getattr(flags, "completed_home_actions", True):
            bits |= FAKE_REASONS["home_actions"]
    if getattr(member, "pending", False):
        bits |= FAKE_REASONS["pending"]
    return bits


def holds_required(member: Any, required: set[int], match: str) -> bool:
    held = {role.id for role in getattr(member, "roles", [])}
    return required <= held if match == "all" else bool(required & held)


def passes(bits: int, settings: dict) -> bool:
    """Whether a join with these reasons is kept by the server's fake-reason filter. With no reasons picked, every join is kept."""
    picked = 0
    for key in settings.get("fake_reasons") or []:
        picked |= FAKE_REASONS.get(key, 0)
    if not picked:
        return True
    hit = bits & picked == picked if settings.get("fake_match") == "all" else bool(bits & picked)
    return hit if settings.get("fake_mode") == "allow" else not hit
