"""What templates may spend, and the one way to run them away from the event loop.

A template can take a quarter second of steady work. Run on the event loop, that holds up every server, and several at once in threads still starve the loop of the interpreter's lock. So every run goes through
`budgeted_render`: at most two at a time, charged by the processor time it really used. A server has a separate pool for each kind of template, so members' tags can't use up what staff triggers or greetings need, and each person has a share, so one member can't use up a pool alone.

A tag or trigger that runs out of its quarter second goes on a bench for ten minutes, the way a slow pattern does, because running it again would only cost the same. Text a member typed can make someone else's template slow, so a stranger's run
only benches that exact text. The whole template goes when its owner ran it, or when three different people have run it out of time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import weakref
from typing import Any, Callable, TypeVar

from .engine import Scope
from .payload import fit_payload, render_payload

log = logging.getLogger(__name__)

T = TypeVar("T")

# Processor time per minute. An ordinary template takes about a millisecond and the heaviest a quarter second, so only a flood of heavy ones reaches these.
SERVER_SECONDS = 5.0     # for each pool in each server
MEMBER_SECONDS = 1.0     # for each person in each server, across every pool
WINDOW = 60.0
RUN_SLOTS = 2            # threads working on templates at once, across every server
MAX_WAITING = 20         # runs of a tag or trigger that can wait for a slot. The next one is refused, since whoever typed it is better off told than kept waiting.
BENCH_SECONDS = 600.0
BENCH_PEOPLE = 3         # different people who must run a template out of time before it stops for everyone
MAX_WINDOWS = 20_000     # what this remembers before it forgets what's past

MEMBERS, STAFF, MESSAGES, PREVIEWS = "members", "staff", "messages", "previews"


class OverBudget(Exception):
    """This run can't go ahead: the budget is spent, or too many runs are already waiting."""


# (server, pool or "person", person or None) -> (when its minute began, seconds spent in it)
_spent: dict[tuple, tuple[float, float]] = {}
_benched: dict[tuple, float] = {}
_ran_out: dict[tuple, dict[int, float]] = {}


class _Gate:
    def __init__(self) -> None:
        self.slots = asyncio.Semaphore(RUN_SLOTS)
        self.waiting = 0


# A semaphore belongs to the event loop that first waits on it.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Gate] = weakref.WeakKeyDictionary()


def _gate() -> _Gate:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = _Gate()
    return gate


def _forget_past(now: float) -> None:
    if len(_spent) > MAX_WINDOWS:
        for key, (start, _) in list(_spent.items()):
            if now - start >= WINDOW:
                del _spent[key]
    if len(_benched) > MAX_WINDOWS:
        for key, until in list(_benched.items()):
            if now >= until:
                del _benched[key]
    if len(_ran_out) > MAX_WINDOWS:
        _ran_out.clear()


def _add(key: tuple, now: float, seconds: float) -> None:
    start, spent = _spent.get(key, (now, 0.0))
    if now - start >= WINDOW:
        start, spent = now, 0.0
    _spent[key] = (start, spent + seconds)


def _used(key: tuple, now: float) -> float:
    start, spent = _spent.get(key, (now, 0.0))
    return 0.0 if now - start >= WINDOW else spent


def _is_open(guild_id: int, pool: str, member_id: int | None, now: float) -> bool:
    if _used((guild_id, pool, None), now) >= SERVER_SECONDS:
        return False
    return member_id is None or _used((guild_id, "person", member_id), now) < MEMBER_SECONDS


def charge(guild_id: int, pool: str | None, member_id: int | None, seconds: float) -> None:
    """Add to a pool, a person's share, or both."""
    now = time.monotonic()
    if pool is not None:
        _add((guild_id, pool, None), now, seconds)
    if member_id is not None:
        _add((guild_id, "person", member_id), now, seconds)
    _forget_past(now)


def spent(guild_id: int, *, pool: str | None = None, member_id: int | None = None) -> float:
    """What this pool or person has spent this minute."""
    key = (guild_id, "person", member_id) if member_id is not None else (guild_id, pool, None)
    return _used(key, time.monotonic())


async def budgeted_render(work: Callable[[], T], guild_id: int, *, pool: str, member_id: int | None = None) -> T:
    """Run `work` in a thread, in its turn, and charge what it used. Raises OverBudget instead of starting when the budget is spent. Anything `work` raises comes out."""
    if not _is_open(guild_id, pool, member_id, time.monotonic()):
        raise OverBudget
    gate = _gate()
    if pool != MESSAGES and gate.waiting >= MAX_WAITING:  # a raid's greetings wait, since nobody is there to be told
        raise OverBudget
    gate.waiting += 1
    try:
        await gate.slots.acquire()
    finally:
        gate.waiting -= 1
    try:
        # Runs that went first may have spent what was left while this one waited.
        if not _is_open(guild_id, pool, member_id, time.monotonic()):
            raise OverBudget
        cost = [0.0]

        def timed() -> T:
            # The clock only runs while this thread works, since waiting for a turn on a busy bot isn't this server's doing.
            began = time.thread_time()
            try:
                return work()
            finally:
                cost[0] = time.thread_time() - began

        try:
            return await asyncio.to_thread(timed)
        finally:
            charge(guild_id, pool, member_id, cost[0])
    finally:
        gate.slots.release()


def _holds_block(value: Any) -> bool:
    if isinstance(value, str):
        return "{" in value
    if isinstance(value, dict):
        return any(_holds_block(item) for item in value.values())
    if isinstance(value, list):
        return any(_holds_block(item) for item in value)
    return False


async def render_message(payload: dict, scope: Scope, *, guild_id: int | None = None) -> dict:
    """Fill in a greeting, level-up, sticky, feed post or other message a server wrote, away from the event loop. Never raises.

    Past the server's budget the template goes out as written, which is what a template past its own limits does. Give `guild_id` when the scope has no guild.
    """
    if not _holds_block(payload):
        return fit_payload(payload)
    place = guild_id if guild_id is not None else getattr(scope.guild, "id", 0)
    try:
        return await budgeted_render(lambda: render_payload(payload, scope), place, pool=MESSAGES)
    except OverBudget:
        log.info("Server %s has spent its template time for this minute, so a message went out as written", place)
        return fit_payload(payload)


def _digest(value: str) -> bytes:
    return hashlib.blake2b(value.encode("utf-8", "surrogatepass"), digest_size=16).digest()


def template_key(guild_id: int, payload: Any) -> tuple:
    """A stored message's identity here. A new version of the words is a new template, so editing a slow tag lifts its bench."""
    return guild_id, _digest(json.dumps(payload, sort_keys=True, default=str))


def is_benched(template: tuple, args: str) -> bool:
    now = time.monotonic()
    for key in (template, (*template, _digest(args))):
        until = _benched.get(key)
        if until is not None:
            if now < until:
                return True
            del _benched[key]
    return False


def bench(template: tuple, args: str, person_id: int | None, *, is_owner: bool) -> bool:
    """A run of this template ran out of time. True when that stops the whole template for everyone, which its owner should hear about once."""
    now = time.monotonic()
    _forget_past(now)
    _benched[(*template, _digest(args))] = now + BENCH_SECONDS
    people = _ran_out.setdefault(template, {})
    people[person_id or 0] = now
    recent = {person: when for person, when in people.items() if now - when < BENCH_SECONDS}
    _ran_out[template] = recent
    if not is_owner and len(recent) < BENCH_PEOPLE:
        return False
    _benched[template] = now + BENCH_SECONDS
    _ran_out.pop(template, None)
    return True


__all__ = [
    "BENCH_SECONDS", "MAX_WAITING", "MEMBERS", "MEMBER_SECONDS", "MESSAGES", "OverBudget", "PREVIEWS", "RUN_SLOTS", "SERVER_SECONDS", "STAFF", "WINDOW",
    "bench", "budgeted_render", "charge", "is_benched", "render_message", "spent", "template_key",
]
