"""Work that has to happen at a future time and survive a restart: unbans, unmutes, reminders, autofeeds.

A module registers a handler for its kind of timer and schedules rows. The loop sleeps until the next one is due.
A row is deleted only after its handler returns, so a crash runs it again on restart. Write handlers so a second
run does no harm. A handler that raises is retried with a growing delay, and after MAX_TRIES once an hour until it works,
because dropping the row could leave someone banned or muted for good.

A row that belongs to a server waits while that server is away. The whole loop pauses while the bot refills its member and
channel lists, since a member missing from a half-filled list looks like someone who left. When the bot is out of a server for
LEFT_KEEP_SECONDS, that server's rows go. Coming back sooner keeps them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from ..db import Database

log = logging.getLogger(__name__)

MIGRATIONS = [
    """
    CREATE TABLE timers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        kind TEXT NOT NULL,
        key TEXT,
        due_at REAL NOT NULL,
        payload TEXT NOT NULL,
        tries INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE INDEX timers_due ON timers (due_at);
    CREATE INDEX timers_key ON timers (kind, key);
    """,
]

MAX_TRIES = 8
MAX_SLEEP = 300.0
LATE_RETRY_SECONDS = 3600
AWAY_SECONDS = 300
LEFT_KEEP_SECONDS = 30 * 86400
STOP_WAIT = 10.0
Handler = Callable[[dict, dict], Awaitable[None]]


class Timers:
    def __init__(self, db: Database, *, server_here: Callable[[int], bool] | None = None, server_left: Callable[[int], bool] | None = None) -> None:
        self.db = db
        self.handlers: dict[str, Handler] = {}
        self.server_here = server_here
        self.server_left = server_left
        self._anywhere: set[str] = set()
        self._held: set[int] = set()
        self._wake = asyncio.Event()
        self._unpaused = asyncio.Event()
        self._unpaused.set()
        self._task: asyncio.Task | None = None

    async def load(self) -> None:
        await self.db.migrate("timers", MIGRATIONS)

    def handler(self, kind: str, handler: Handler, *, anywhere: bool = False) -> None:
        """`handler(payload, row)` runs when a timer of this kind comes due. `anywhere` is for work that doesn't need its server to be here, such as a reminder sent by DM."""
        self.handlers[kind] = handler
        if anywhere:
            self._anywhere.add(kind)

    async def schedule(self, kind: str, due_at: float, payload: dict | None = None, *, guild_id: int | None = None, key: str | None = None) -> int:
        """Add a timer. With a key, any earlier timer of this kind and key is replaced."""
        if key is not None:
            await self.db.run("DELETE FROM timers WHERE kind = ? AND key = ?", (kind, key))
        timer_id = await self.db.insert(
            "INSERT INTO timers (guild_id, kind, key, due_at, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, kind, key, due_at, json.dumps(payload or {}), time.time()),
        )
        self._wake.set()
        return timer_id

    def nudge(self) -> None:
        """Look at the table again now. For code that wrote timer rows itself, such as restoring a backup."""
        self._wake.set()

    async def cancel(self, timer_id: int) -> bool:
        return await self.db.run("DELETE FROM timers WHERE id = ?", (timer_id,)) > 0

    async def cancel_key(self, kind: str, key: str) -> int:
        return await self.db.run("DELETE FROM timers WHERE kind = ? AND key = ?", (kind, key))

    async def get_key(self, kind: str, key: str) -> dict | None:
        row = await self.db.one("SELECT * FROM timers WHERE kind = ? AND key = ?", (kind, key))
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    async def list_kind(self, kind: str, guild_id: int | None = None) -> list[dict]:
        if guild_id is None:
            rows = await self.db.all("SELECT * FROM timers WHERE kind = ? ORDER BY due_at", (kind,))
        else:
            rows = await self.db.all("SELECT * FROM timers WHERE kind = ? AND guild_id = ? ORDER BY due_at", (kind, guild_id))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def pause(self) -> None:
        """Run nothing new until `resume`. A handler already running finishes."""
        self._unpaused.clear()

    def resume(self) -> None:
        self._unpaused.set()
        self._wake.set()

    def hold(self, guild_id: int) -> None:
        """Run nothing of this server's until `release`, for a restore that's replacing the rows its timers read."""
        self._held.add(guild_id)

    def release(self, guild_id: int) -> None:
        self._held.discard(guild_id)
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        """Stop the loop. A handler gets STOP_WAIT seconds to put things back, and then it's cut off, because a stop still has everything in memory to save. Its row stays for the next start."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        _, left = await asyncio.wait({task}, timeout=STOP_WAIT)
        if left:
            log.warning("A timer handler was still putting things back after %d seconds, so it was cut off.", STOP_WAIT)
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)

    async def run_due(self, now: float | None = None) -> int:
        """Run everything due now. The loop calls this, and so can a test."""
        now = time.time() if now is None else now
        if not self._unpaused.is_set():
            return 0
        passed_over, held = self._passed_over()
        rows = await self.db.all(f"SELECT * FROM timers WHERE due_at <= ? {passed_over} ORDER BY due_at LIMIT 200", (now, *held))
        for row in rows:
            if not self._unpaused.is_set():
                break
            await self._run_one(row)
        return len(rows)

    def _passed_over(self) -> tuple[str, tuple[int, ...]]:
        """SQL that leaves out servers on hold, so their rows can't fill a batch ahead of every other server's, or keep the loop waking for work it won't do."""
        if not self._held:
            return "", ()
        return f"AND (guild_id IS NULL OR guild_id NOT IN ({', '.join('?' * len(self._held))}))", tuple(self._held)

    async def _run_one(self, batched: dict) -> None:
        # The batch was read a while ago. A row canceled or moved since then has to stay that way, or a permanent ban could be undone by the old temporary one.
        row = await self.db.one("SELECT * FROM timers WHERE id = ?", (batched["id"],))
        if row is None or row["due_at"] != batched["due_at"] or row["guild_id"] in self._held:
            return
        handler = self.handlers.get(row["kind"])
        if handler is None:
            # The module that owned this kind isn't loaded. Look again later instead of losing the work.
            await self.db.run("UPDATE timers SET due_at = ? WHERE id = ?", (time.time() + 3600, row["id"]))
            return
        if row["guild_id"] is not None and row["kind"] not in self._anywhere and self.server_here is not None and not self.server_here(row["guild_id"]):
            # The server is unavailable, or the bot isn't in it. Neither means the work is over yet, and a handler would read the empty lists as everyone having left.
            if await self._gone_for_good(row["guild_id"]):
                return
            await self.db.run("UPDATE timers SET due_at = ? WHERE id = ?", (time.time() + AWAY_SECONDS, row["id"]))
            return
        try:
            await handler(json.loads(row["payload"]), row)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # stop() wants this task to end
                raise
            await self._failed(row)  # something the handler awaited was canceled by someone else, which is only this row's failure
            return
        except Exception:
            await self._failed(row)
            return
        await self.db.run("DELETE FROM timers WHERE id = ? AND due_at = ?", (row["id"], row["due_at"]))

    async def _failed(self, row: dict) -> None:
        tries = row["tries"] + 1
        if tries >= MAX_TRIES:
            log.exception("Timer %s (%s) has failed %d times. It stays and tries again in an hour.", row["id"], row["kind"], tries)
            delay = LATE_RETRY_SECONDS
        else:
            log.exception("Timer %s (%s) failed, try %d", row["id"], row["kind"], tries)
            delay = 30 * 2 ** tries
        await self.db.run("UPDATE timers SET tries = ?, due_at = ? WHERE id = ?", (tries, time.time() + delay, row["id"]))

    async def _gone_for_good(self, guild_id: int) -> bool:
        """Notes when the bot was first found out of a server, and removes that server's timers once that's long past."""
        if self.server_left is None or not self.server_left(guild_id):
            return False
        key = f"server_left:{guild_id}"
        stamp = await self.db.scalar("SELECT value FROM kv WHERE key = ?", (key,))
        if stamp is None:
            await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?)", (key, str(time.time())))
            return False
        if time.time() - float(stamp) < LEFT_KEEP_SECONDS:
            return False
        removed = await self.db.run("DELETE FROM timers WHERE guild_id = ?", (guild_id,))
        await self.db.run("DELETE FROM kv WHERE key = ?", (key,))
        log.warning("The bot left server %s more than %d days ago, so its %d timers were removed.", guild_id, LEFT_KEEP_SECONDS // 86400, removed)
        return True

    async def server_back(self, guild_id: int) -> None:
        """The bot is in this server again, so the count toward removing its timers starts over."""
        await self.db.run("DELETE FROM kv WHERE key = ?", (f"server_left:{guild_id}",))

    async def _loop(self) -> None:
        while True:
            try:
                await self._unpaused.wait()
                self._wake.clear()  # before the reads below, so a schedule that lands after them still wakes this loop
                await self.run_due()
                passed_over, held = self._passed_over()
                next_due = await self.db.scalar(f"SELECT MIN(due_at) FROM timers WHERE 1 = 1 {passed_over}", held)
                delay = MAX_SLEEP if next_due is None else max(0.05, min(MAX_SLEEP, next_due - time.time()))
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Timer loop error")
                await asyncio.sleep(5)
