"""SQLite storage, encrypted with SQLCipher, with write-ahead logging.

Every helper returns plain Python values: `one` and `all` give dicts. Each module owns its tables and
brings them in as an ordered list of migration steps through `migrate`. A step never changes after it has
shipped: add a new step instead.

Writes are serialized by one lock, so a `transaction()` block can't have another task's write fall into it.

Reads share the writer's connection and take no lock, so during another task's transaction they can see its uncommitted rows,
including rows it then rolls back. That's fine for showing something. A decision that must not act on such a row reads inside
`transaction()`, which waits its turn.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager

import aiosqlite
from sqlcipher3 import dbapi2 as cipher

from .crypto import hkdf_sha256

log = logging.getLogger(__name__)

CIPHER_COMPATIBILITY = 4
WAL_LIMIT_BYTES = 8 * 1024 * 1024
DETACH_TRIES = 1000

Step = "str | Callable[[Database], Awaitable[None]]"

# Which databases have a transaction open here, and the task holding each. The bot has two, and a transaction on one must not make writes to the other skip their own.
# A task started inside a block copies this along with the rest of its context, so the task is named: that child waits for the lock like anyone else.
_in_transaction: contextvars.ContextVar[dict] = contextvars.ContextVar("in_transaction", default={})


class DatabaseKeyError(Exception):
    """The file exists and this key doesn't open it."""


def _open_encrypted(target: str, key: str, **options: Any) -> cipher.Connection:
    connection = cipher.connect(target, check_same_thread=False, **options)
    try:
        connection.execute(f"PRAGMA key = \"x'{key}'\"").fetchall()
        connection.execute(f"PRAGMA cipher_compatibility = {CIPHER_COMPATIBILITY}").fetchall()
        connection.execute("SELECT count(*) FROM sqlite_master").fetchall()
    except cipher.DatabaseError as error:
        connection.close()
        if "not a database" not in str(error):  # a locked file or a failed read isn't a wrong key, and a restart can fix those
            raise
        raise DatabaseKeyError(f"{target} can't be opened with this key") from error
    return connection


class Database:
    def __init__(self, path: Path | str, key: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", key):  # it goes into a PRAGMA, which can't take a bound value
            raise ValueError("database key must be 64 lowercase hex digits")
        self.path = str(path)
        self._key = key
        self.conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        """Opens the file. When anything after connecting fails, a cancellation included, the connection is stopped before the error
        goes on: its worker thread isn't a daemon, and one left running keeps the process from exiting."""
        conn = await aiosqlite.Connection(lambda: _open_encrypted(self.path, self._key), 64)
        try:
            conn.row_factory = cipher.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA secure_delete=ON")
            await conn.execute(f"PRAGMA journal_size_limit={WAL_LIMIT_BYTES}")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_steps (namespace TEXT NOT NULL, step INTEGER NOT NULL, applied_at REAL NOT NULL, "
                "PRIMARY KEY (namespace, step))"
            )
            await conn.commit()
        except BaseException:
            conn.stop()  # closes the file on the connection's own thread and ends the thread, with nothing to wait for
            raise
        self.conn = conn

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    def sibling(self, path: Path | str) -> "Database":
        """Another database file under the same key, for something stored beside this one. Call `open` on it."""
        return Database(path, self._key)

    def derived_key(self, purpose: str) -> bytes:
        """A separate 256-bit key tied to this database's key, for sealing something kept outside the file."""
        return hkdf_sha256(bytes.fromhex(self._key), info=purpose.encode())

    @asynccontextmanager
    async def reader(self) -> AsyncIterator["Database"]:
        """A second connection that sees one unchanging copy of the whole file until the block ends.

        With write-ahead logging, writers on the main connection carry on meanwhile, so a long export neither stops the bot nor catches it halfway through a change.
        """
        view = Database(self.path, self._key)
        view.conn = await aiosqlite.Connection(lambda: _open_encrypted(view.path, view._key), 64)
        view.conn.row_factory = cipher.Row
        try:
            await view.conn.execute("PRAGMA query_only=ON")
            await view.conn.execute("BEGIN")
            await view.scalar("SELECT count(*) FROM sqlite_master")  # the view starts at the first read, so it starts here
            yield view
        finally:
            await view.conn.close()

    async def migrate(self, namespace: str, steps: Sequence[Any]) -> None:
        """Apply the steps this database hasn't seen yet, in order. A step is SQL text or an async function."""
        rows = await self.all("SELECT step FROM schema_steps WHERE namespace = ?", (namespace,))
        done = {row["step"] for row in rows}
        for index, step in enumerate(steps):
            if index in done:
                continue
            async with self.transaction():
                if callable(step):
                    await step(self)
                else:
                    for statement in _split_statements(step):
                        await self.conn.execute(statement)
                await self.conn.execute(
                    "INSERT INTO schema_steps (namespace, step, applied_at) VALUES (?, ?, ?)", (namespace, index, time.time())
                )

    def _joined(self) -> bool:
        return _in_transaction.get().get(self) is asyncio.current_task()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["Database"]:
        """One atomic group of writes. Nesting joins the outer one. A task started inside the block isn't part of it."""
        if self._joined():
            yield self
            return
        async with self._write_lock:
            token = _in_transaction.set({**_in_transaction.get(), self: asyncio.current_task()})
            try:
                # A canceled task's BEGIN still runs on the database's thread, so it has to be inside the block that rolls back.
                await self.conn.execute("BEGIN IMMEDIATE")
                yield self
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
            finally:
                _in_transaction.reset(token)

    async def exclusive(self, work: Callable[[Any], Any], *, attach: tuple[Path, str] | None = None, cache_kib: int | None = None) -> Any:
        """One atomic group of writes that nothing can land inside, not even a read on this connection. `work` runs on the database's own thread with the raw connection, so it can't await.

        `attach` opens another file under this key beside the main one for the length of the work. `cache_kib` holds more pages in memory for that long,
        because a big change that has to keep writing pages out and reading them back takes several times as long, with everything else waiting.
        """
        if self._joined():
            raise RuntimeError("exclusive() can't run inside a transaction, because it would wait on its own lock.")

        def run() -> Any:
            connection = self.conn._conn
            cache_size = connection.execute("PRAGMA cache_size").fetchone()[0]
            if cache_kib is not None:
                connection.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
            if attach is not None:
                connection.execute(f"ATTACH DATABASE ? AS {attach[1]} KEY \"x'{self._key}'\"", (str(attach[0]),))
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = work(connection)
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise
            finally:
                connection.execute(f"PRAGMA cache_size={int(cache_size)}")

        async with self._write_lock:
            try:
                return await self.conn._execute(run)
            finally:
                if attach is not None:
                    await self._detach(attach[1])

    async def _detach(self, name: str) -> None:
        """Another task's read that's under way holds an attached file until it finishes, which takes moments."""
        for _ in range(DETACH_TRIES):
            try:
                cursor = await self.conn.execute(f"DETACH DATABASE {name}")
                await cursor.close()
                return
            except cipher.OperationalError as error:
                if "no such database" in str(error):
                    return
                await asyncio.sleep(0.01)
        log.error("Could not let go of %s on %s", name, self.path)

    async def _write(self, action: Callable[[], Awaitable[Any]]) -> Any:
        if self._joined():
            return await action()
        async with self._write_lock:
            try:
                result = await action()
                await self.conn.commit()
                return result
            except BaseException:
                # A failed or canceled statement leaves its implicit transaction open, and the next BEGIN would refuse to start.
                await self.conn.rollback()
                raise

    async def run(self, sql: str, args: Iterable[Any] = ()) -> int:
        """Run one write and return how many rows it changed."""
        async def action() -> int:
            cursor = await self.conn.execute(sql, tuple(args))
            count = cursor.rowcount
            await cursor.close()
            return count
        return await self._write(action)

    async def insert(self, sql: str, args: Iterable[Any] = ()) -> int:
        """Run one INSERT and return the new row's id."""
        async def action() -> int:
            cursor = await self.conn.execute(sql, tuple(args))
            row_id = cursor.lastrowid
            await cursor.close()
            return row_id
        return await self._write(action)

    async def many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        async def action() -> None:
            await self.conn.executemany(sql, [tuple(row) for row in rows])
        await self._write(action)

    async def one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        cursor = await self.conn.execute(sql, tuple(args))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        cursor = await self.conn.execute(sql, tuple(args))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def scalar(self, sql: str, args: Iterable[Any] = ()) -> Any:
        cursor = await self.conn.execute(sql, tuple(args))
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row is not None else None


def _split_statements(script: str) -> list[str]:
    """Split a migration into statements. cipher.complete_statement knows about semicolons inside strings and triggers."""
    statements, current = [], ""
    for line in script.splitlines(keepends=True):
        current += line
        if cipher.complete_statement(current):
            if current.strip():
                statements.append(current.strip())
            current = ""
    if current.strip():
        statements.append(current.strip())
    return statements
