"""Everything stored for a server as one file, put back after a preview, and snapshots taken on a schedule.

What goes in the file is read from the databases' own schema when it's made, so a new module's tables are included with no change
here. A table belongs to a server when it has a guild_id column, or a foreign key leading to a table that does. A table with neither
and no such link inside its own module can't be tied to a server, and the file names it under "left_out".

Files can be much larger than memory, so they're written and read as streams, row by row, and only a few jobs run at once across all servers. Snapshots and uploads waiting for a decision
sit in encrypted files under the data folder, in the same SQLCipher format as the main database, so the encryption of the data at rest
has no gap. A file someone downloads is plain JSON, except that secrets are sealed under a key only this install holds.

A file comes from a person, so nothing in it is trusted. Every row is forced into this server, settings go through their own checks,
and a row that points at another server's channel or another server's parent row is dropped.

A restore checks the file's rows into a holding file first, then swaps them in with one short transaction, so every server keeps working
while a big file is read and nothing ever sees a table half replaced.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

from aiohttp import web
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlcipher3 import dbapi2 as cipher

from .. import __version__, brand
from ..core.module import Module
from ..core.validate import SECRET_MASK, Field, Schema, ValidationError, boolean, choice, integer
from ..db import Database, DatabaseKeyError
from ..web.api import ApiError, safe_json
from ..web.app import SECURITY_HEADERS, field_naming_another_server

log = logging.getLogger(__name__)

FORMAT = f"{brand.SLUG}-backup"
VERSION = 1
BATCH_ROWS = 500
BATCH_CHARS = 8 * 1024 * 1024  # rows can be long, so a batch also ends at this much text
CHUNK_BYTES = 1024 * 1024
INFLATE_BLOCK = 1024 * 1024
MAX_UPLOAD_BYTES = 16 * 1024 ** 3
MAX_INFLATED_BYTES = 128 * 1024 ** 3  # a small file can claim to expand to almost anything
MAX_VALUE_CHARS = 16 * 1024 * 1024
MAX_STRING_CHARS = 256 * 1024 * 1024  # one text cell, such as a large server's level backup. Reading a long string costs no more than its length.
MAX_ROW_CHARS = 512 * 1024 * 1024
MAX_ROW_CELLS = 1024
BATCH_CELLS = 20_000  # a batch is the only place the reader lets other work run, so it also ends at this many cells
ROW_WINDOW_CHARS = 16 * 1024  # a row this short is read in one go, which is far quicker for a table of a million rows, and can't cost much whatever it holds
NESTED_CELL_CHARS = 4096  # what a cell that isn't plain can hold. Building nested lists is what costs memory and time, and no column holds one.
MAX_JSON_CHECK_CHARS = 4 * 1024 * 1024  # text longer than this is left unparsed when looking for another server's IDs
MIN_FREE_BYTES = 2 * 1024 ** 3  # a full disk stops the main database for every server, so backups stop well before that
MAX_JOBS_AT_ONCE = 2  # across all servers, since each one reads a whole file or a whole server
MAX_UPLOADS_AT_ONCE = 2
MAX_DOWNLOADS_AT_ONCE = 8
MAX_RESTORE_ROWS = 5_000_000  # main-database rows in one restore. The swap holds every server's database work, and a million rows take from 3 to 12 seconds.
MAX_SNAPSHOT_BYTES = 64 * 1024 ** 3  # one server's snapshots on disk. The oldest scheduled ones go first.
MAX_HEADER_CHARS = 1024 * 1024  # one value outside the rows and the settings, where a real file holds only small things
MAX_HEADER_TOTAL = 4 * 1024 * 1024
MAX_LINKED_IDS = 2_000_000  # the row IDs kept in memory so that children can be checked against their parents
MAX_WARNINGS = 200
DIFF_ROW_LIMIT = 200_000  # beyond this a preview shows row counts only, since comparing rows would take too much memory
RESTORING_KEY = "backup_restoring:"  # stays in kv from just before the swap until the settings are back, so a restart in between can say so
SWEEP_KIND = "backup.sweep"
SWEEP_SECONDS = 3600
FIRST_SWEEP_SECONDS = 900
UPLOAD_SECONDS = 6 * 3600
MANUAL_KEEP = 25
BEFORE_IMPORT_KEEP = 5
CHANGE_PREVIEW_CHARS = 300
JS_SAFE_INT = 2 ** 53 - 1
SEAL_PURPOSE = "backup/secrets"
HOLDING = "holding"  # the name a restore's holding file goes by while it's attached to the main database
SWAP_CACHE_KIB = 256 * 1024  # a swap of a million rows took 12 seconds on the normal 2 MB of pages and 3 on this much, with every server waiting
MARK_PURPOSE = "backup/rows"
MARK_COLUMN = "$mark"
# A row in these needs proof that this install wrote it. A timer runs on its own later, and its payload often names a row by a number the handler then acts on, whichever server
# owns it, so one written by hand could reach into another server. The activity log says who changed what, so one written by hand could blame someone who did nothing.
# A reminder makes the bot DM its text to the person it names, so one written by hand could send anyone anything.
MARKED_TABLES = {
    "main.timers": ("kind", "key", "payload"),
    "main.reminders": ("user_id", "about", "repeat_seconds", "jump_url"),
    "main.activity": ("at", "user_id", "user_name", "action", "detail", "module", "before_data", "after_data"),
    "main.tags": ("name", "content", "extra", "channel_wl", "channel_bl", "role_wl", "role_bl", "trusted"),
    "main.triggers": ("match_type", "phrase", "content", "extra", "channel_id", "channel_wl", "channel_bl", "role_wl", "role_bl", "trusted"),
}
# These rows keep their place whatever a file says, but one flag needs the proof. Trust lets a template act with the bot's permissions, so a file edited by hand comes back with the words and without the trust.
FLAG_WITHOUT_MARK = {"main.tags": "trusted", "main.triggers": "trusted"}
BOM = chr(0xFEFF)

KIND_SCHEDULED, KIND_MANUAL, KIND_BEFORE_IMPORT = "scheduled", "manual", "before import"

# Not one server's data, or handled on their own: settings go through their schemas, and a snapshot's list describes files on this machine.
SPECIAL_TABLES = {"guild_settings", "backup_snapshots"}
# youtube_state is the polling place for one YouTube channel, shared by every server following it, and the next poll rebuilds it.
# voicelink_held, autorole_waiting, raid_modes and raid_holds say how things stand right now, and an older picture would take roles from people or hand them out. Voice states, the rules screen and staff keep them right.
# wiki_copies and wiki_pages hold copies of public wikis, shared by every server that reads the same one, and the next update downloads them again.
GLOBAL_TABLES = {"kv", "web_sessions", "schema_steps", "sqlite_sequence", "youtube_state", "voicelink_held", "autorole_waiting", "raid_modes", "raid_holds", "wiki_copies", "wiki_pages"}
# A history keeps what's in it when a backup comes back: only what's missing gets added. Replacing the activity log would also let a file erase who changed what.
# The same goes for what AI answers cost and how many each person asked for today: an older file must not lower either.
# And for the forum posts welcomed and reminded: an older file must not forget one, or it would be welcomed or reminded twice.
MERGED_TABLES = {"activity", "timers", "command_uses", "answer_costs", "answer_uses", "answer_auto_days", "forum_welcomes", "forum_reminders"}
ACTIVITY_JSON_COLUMNS = ("before_data", "after_data")
_SECRET_COLUMN = re.compile(r"(?:^|_)(?:token|secret|password|api_key)(?:$|_)", re.IGNORECASE)
_DIGITS = re.compile(r"-?[0-9]{1,20}")
_MARKED_CELL = re.compile(r'\{\s*"(\$(?:b64|int|float|sealed))"\s*:\s*(?=")')
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)", re.IGNORECASE)
CORE_OWNERS = {"activity": "activity", "timers": "scheduled work", "command_uses": "command use"}


class BackupError(Exception):
    """The file can't be used, or the work can't finish. The text is for the person."""


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def affinity_of(declared: str) -> str:
    """SQLite's own rules for what a column's declared type means. See Datatypes in SQLite, section 3.1."""
    kind = (declared or "").upper()
    if "INT" in kind:
        return "integer"
    if any(word in kind for word in ("CHAR", "CLOB", "TEXT")):
        return "text"
    if "BLOB" in kind or not kind:
        return "blob"
    if any(word in kind for word in ("REAL", "FLOA", "DOUB")):
        return "real"
    return "numeric"


# ---- the shape of the databases ----

@dataclass
class Link:
    """This table's column points at a parent table's column. `declared` is false when it was inferred, so SQLite doesn't enforce it."""
    column: str
    parent: str
    parent_column: str
    declared: bool = True


@dataclass
class Table:
    database: str
    name: str
    columns: list[str]
    affinity: dict[str, str]
    primary_key: list[str]
    required: list[str]
    links: list[Link]
    where: str
    rowid_key: str | None
    owner: str
    group: str = ""
    autoincrement: bool = False

    @property
    def key(self) -> str:
        return f"{self.database}.{self.name}"

    @property
    def merges(self) -> bool:
        return self.database != "main" or self.name in MERGED_TABLES


@dataclass
class Catalog:
    tables: dict[str, Table]
    left_out: list[str]

    def referenced_columns(self, table: Table) -> set[str]:
        """The columns of this table that another table's rows point at."""
        return {
            link.parent_column
            for other in self.tables.values() if other.database == table.database
            for link in other.links if link.parent == table.name
        }

    def ordered(self, database: str | None = None) -> list[Table]:
        """Parents before their children, so a row's parent is already in place when the row arrives."""
        pending = {t.key: t for t in self.tables.values() if database is None or t.database == database}
        done: list[Table] = []
        placed: set[str] = set()
        while pending:
            ready = [t for t in pending.values() if all(f"{t.database}.{link.parent}" in placed or f"{t.database}.{link.parent}" not in pending or link.parent == t.name for link in t.links)]
            for table in sorted(ready or pending.values(), key=lambda t: t.key):  # a loop of foreign keys goes in name order
                done.append(table)
                placed.add(table.key)
                pending.pop(table.key)
        return done


def table_owners(bot: Any) -> dict[str, str]:
    """Which module made each table, from the CREATE statements in its migrations. Only used for labels and for inferring links."""
    owners = dict(CORE_OWNERS)
    for module in getattr(bot, "modules", []):
        for step in [*module.migrations, *module.message_migrations]:
            if isinstance(step, str):
                for name in _CREATE_TABLE.findall(step):
                    owners[name] = module.key
    return owners


def child_links(bot: Any) -> dict[str, dict[str, str]]:
    """What each module says about rows that hang off another table's row with no foreign key. See Module.child_links."""
    links: dict[str, dict[str, str]] = {}
    for module in getattr(bot, "modules", []):
        for table, columns in module.child_links.items():
            links.setdefault(table, {}).update(columns)
    return links


async def read_catalog(database: Database, label: str, owners: dict[str, str], children: dict[str, dict[str, str]] | None = None) -> Catalog:
    listing = await database.all("PRAGMA table_list")
    created = {row["name"]: row["sql"] or "" for row in await database.all("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    columns_of: dict[str, list[dict]] = {}
    links_of: dict[str, list[Link]] = {}
    no_rowid: dict[str, bool] = {}
    # Views, virtual tables and their shadow tables hold nothing of their own. A search index rebuilds through its base table's triggers.
    for row in listing:
        name = row["name"]
        if row["schema"] != "main" or row["type"] != "table" or name.startswith("sqlite_") or name in SPECIAL_TABLES or name in GLOBAL_TABLES:
            continue
        columns_of[name] = [c for c in await database.all(f"PRAGMA table_xinfo({quote(name)})") if c["hidden"] == 0]
        no_rowid[name] = bool(row["wr"])
        links_of[name] = []
    for name in columns_of:
        keys: dict[int, list[dict]] = {}
        for link in await database.all(f"PRAGMA foreign_key_list({quote(name)})"):
            keys.setdefault(link["id"], []).append(link)
        for rows in keys.values():
            if len(rows) != 1 or rows[0]["table"] not in columns_of:
                continue  # a key over several columns can't scope a table, and SQLite still enforces it
            parent = rows[0]["table"]
            parent_pk = [c["name"] for c in sorted(columns_of[parent], key=lambda c: c["pk"]) if c["pk"]]
            target = rows[0]["to"] or (parent_pk[0] if len(parent_pk) == 1 else None)
            if target:
                links_of[name].append(Link(rows[0]["from"], parent, target))
    for name, columns in (children or {}).items():
        for column, target in columns.items():
            parent, _, parent_column = target.partition(".")
            if name in columns_of and parent in columns_of:
                links_of[name].append(Link(column, parent, parent_column, declared=False))

    where: dict[str, str] = {}
    for name, columns in columns_of.items():
        if any(c["name"] == "guild_id" for c in columns):
            where[name] = "guild_id = ?"

    def follow_links() -> None:
        changed = True
        while changed:
            changed = False
            for name in columns_of:
                if name in where:
                    continue
                for link in links_of[name]:
                    if link.parent in where:
                        where[name] = f"{quote(link.column)} IN (SELECT {quote(link.parent_column)} FROM {quote(link.parent)} WHERE {where[link.parent]})"
                        changed = True
                        break

    follow_links()
    # Some tables hang off another in the same module with no declared key, for example a list of what was seen per message. Sharing the parent's own key column names the link.
    for name, columns in columns_of.items():
        if name in where or owners.get(name) is None:
            continue
        names = {c["name"] for c in columns}
        for parent, parent_columns in columns_of.items():
            if parent == name or parent not in where or owners.get(parent) != owners[name]:
                continue
            pk = [c["name"] for c in parent_columns if c["pk"]]
            # "id" says nothing about what a column points at, while a name like message_id does.
            if len(pk) == 1 and pk[0] != "id" and pk[0] in names:
                links_of[name].append(Link(pk[0], parent, pk[0], declared=False))
                break
    follow_links()

    tables: dict[str, Table] = {}
    left_out: list[str] = []
    for name, columns in columns_of.items():
        if name not in where:
            left_out.append(f"{label}.{name}")
            continue
        pk = [c["name"] for c in sorted(columns, key=lambda c: c["pk"]) if c["pk"]]
        single_integer = len(pk) == 1 and next(c for c in columns if c["name"] == pk[0])["type"].upper() == "INTEGER"
        table = Table(
            database=label,
            name=name,
            columns=[c["name"] for c in columns],
            affinity={c["name"]: affinity_of(c["type"]) for c in columns},
            primary_key=pk,
            required=[c["name"] for c in columns if c["notnull"] and c["dflt_value"] is None and not (single_integer and c["name"] == pk[0])],
            links=[link for link in links_of[name] if link.parent in where],
            where=where[name],
            rowid_key=pk[0] if single_integer and not no_rowid[name] else None,
            owner=owners.get(name, "other"),
            autoincrement="AUTOINCREMENT" in created.get(name, "").upper(),
        )
        tables[table.key] = table
    _group_linked(tables)
    return Catalog(tables, sorted(left_out))


def _group_linked(tables: dict[str, Table]) -> None:
    """Tables tied by keys restore as one unit, because deleting a parent takes its children along."""
    root = {key: key for key in tables}

    def find(key: str) -> str:
        while root[key] != key:
            root[key] = root[root[key]]
            key = root[key]
        return key

    for table in tables.values():
        for link in table.links:
            parent = f"{table.database}.{link.parent}"
            if parent in root:
                root[find(table.key)] = find(parent)
    for table in tables.values():
        table.group = find(table.key)


# ---- turning cells into JSON and back ----

class BadCell(ValueError):
    """A value in a file that no column could have held."""


def encode_cell(value: Any, affinity: str) -> Any:
    """A column's value as JSON. A big number goes as text, as the rest of this project sends IDs, because many JSON readers round it."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        if abs(value) <= JS_SAFE_INT:
            return value
        return str(value) if affinity in ("integer", "numeric") else {"$int": str(value)}
    if isinstance(value, float):
        return value if math.isfinite(value) else {"$float": repr(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError(f"Can't back up {type(value).__name__}")


def decode_cell(value: Any, affinity: str) -> Any:
    if isinstance(value, dict):
        try:
            if set(value) == {"$b64"}:
                if affinity != "blob":  # code that reads a text column expects text
                    raise BadCell("Only a blob column holds bytes")
                value = base64.b64decode(value["$b64"], validate=True)
            elif set(value) == {"$int"}:
                value = int(value["$int"])
            elif set(value) == {"$float"}:
                value = float(value["$float"])
            else:
                raise BadCell("Unknown value")
        except (ValueError, TypeError) as error:
            raise BadCell(str(error)) from error
    elif isinstance(value, list):
        raise BadCell("A cell can't hold a list")
    elif isinstance(value, bool):
        value = int(value)
    if affinity in ("integer", "numeric") and value is not None:
        # SQLite turns " 12", "+12" and 12.0 into the number 12 on the way in, past every check made on them before that.
        if isinstance(value, str) and _DIGITS.fullmatch(value):
            value = int(value)
        elif not isinstance(value, int):
            raise BadCell("A number column holds whole numbers")
    if isinstance(value, int) and not -2 ** 63 <= value < 2 ** 63:
        raise BadCell("A number too large to store")
    if isinstance(value, float) and not math.isfinite(value):  # a browser can't read one in JSON, and code comparing times with it never ends
        raise BadCell("Not a finite number")
    if affinity == "real" and value is not None and not isinstance(value, (int, float)):
        raise BadCell("A real column holds numbers")
    return value


def dump_json(value: Any) -> bytes:
    # backslashreplace turns a lone surrogate into the same \ud83d form JSON uses, so the file stays valid UTF-8 whatever a message held.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8", "backslashreplace")


# ---- reading a file of any size ----

class JsonStream:
    """One JSON document read piece by piece from blocks of bytes, so rows can be handled without holding the file in memory."""

    def __init__(self, blocks: AsyncIterator[bytes]) -> None:
        self._blocks = blocks.__aiter__()
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._json = json.JSONDecoder()
        self._text = ""
        self._at = 0
        self._ended = False
        self._started = False
        self.last_chars = 0
        self.last_cells = 0

    async def _more(self) -> bool:
        if self._ended:
            return False
        try:
            block = await self._blocks.__anext__()
            final = False
        except StopAsyncIteration:
            block, final = b"", True
        try:
            piece = self._decoder.decode(block, final)
        except UnicodeDecodeError as error:
            raise BackupError("This file isn't readable text.") from error
        if final:
            self._ended = True
        if not self._started and piece:
            self._started = True
            piece = piece.removeprefix(BOM)
        self._text = self._text[self._at:] + piece
        self._at = 0
        return True

    async def peek(self) -> str:
        while True:
            while self._at < len(self._text) and self._text[self._at] in " \t\r\n":
                self._at += 1
            if self._at < len(self._text):
                return self._text[self._at]
            if not await self._more():
                raise BackupError("The file ends early. It may not have finished downloading.")

    async def expect(self, character: str) -> None:
        if await self.peek() != character:
            raise BackupError("This file isn't in the backup format.")
        self._at += 1

    def skip(self) -> None:
        self._at += 1

    async def value(self, limit: int = MAX_VALUE_CHARS) -> Any:
        await self.peek()
        while True:
            try:
                value, end = self._json.raw_decode(self._text, self._at)
            except json.JSONDecodeError as error:
                # Running out of text and a real mistake raise the same error. A mistake sits well before the end, or isn't a cut-off string.
                near_end = error.pos >= len(self._text) - 16 or error.msg.startswith("Unterminated string")
                if not near_end:
                    raise BackupError("The file is damaged or isn't in the backup format.") from error
                if self._ended:
                    raise BackupError("The file ends early. It may not have finished downloading.") from error
                if len(self._text) - self._at > limit:
                    raise BackupError("A value in this file is too large to read.") from error
                # Each try starts the value over, so it waits for twice as much text and a long value costs no more than twice its length.
                wanted = (len(self._text) - self._at) * 2
                while not self._ended and len(self._text) - self._at < wanted:
                    await self._more()
                continue
            except (ValueError, RecursionError) as error:
                raise BackupError("The file is damaged or isn't in the backup format.") from error
            if end == len(self._text) and not self._ended:  # a number could carry on in the next block
                await self._more()
                continue
            if end - self._at > limit:
                raise BackupError("A value in this file is too large to read.")
            self.last_chars = end - self._at
            self._at = end
            return value

    async def _fill(self, wanted: int) -> None:
        while not self._ended and len(self._text) - self._at < wanted:
            await self._more()

    async def row(self) -> Any:
        """One row. A row is a list of flat cells, so a long one is read a cell at a time, and anything nested is refused before it costs more than a little memory. A flood of nested lists is the one shape of JSON that costs far more than its length."""
        await self.peek()
        await self._fill(ROW_WINDOW_CHARS)
        window = self._text[self._at:self._at + ROW_WINDOW_CHARS]
        try:
            value, end = self._json.raw_decode(window)
            whole = end < len(window) or self._ended  # a number that reaches the end of the window could carry on past it
        except (ValueError, RecursionError):
            whole = False
        if whole:
            self._at += end
            self.last_chars = end
            self.last_cells = len(value) if isinstance(value, list) else 1
            return value
        first = self._text[self._at]
        if first == "[":
            return await self._long_row()
        self.last_cells = 1
        return await self.value(MAX_STRING_CHARS) if first == '"' else await self._small_value()

    async def _long_row(self) -> list:
        self._at += 1
        cells: list = []
        chars = 2
        while True:
            if await self.peek() == "]":
                self._at += 1
                break
            if cells:
                await self.expect(",")
                chars += 1
            cells.append(await self._cell())
            chars += self.last_chars
            if len(cells) > MAX_ROW_CELLS or chars > MAX_ROW_CHARS:
                raise BackupError("A row in this file is too large to read.")
        self.last_chars = chars
        self.last_cells = len(cells)
        return cells

    async def _cell(self) -> Any:
        first = await self.peek()
        if first == "{":
            await self._fill(64)
            head = _MARKED_CELL.match(self._text, self._at)
            if head is not None:
                name, chars = head.group(1), head.end() - self._at
                self._at = head.end()
                value = await self.value(MAX_STRING_CHARS)
                chars += self.last_chars
                await self.expect("}")
                self.last_chars = chars + 1
                return {name: value}
        if first in "{[":
            return await self._small_value()
        return await self.value(MAX_STRING_CHARS)

    async def _small_value(self) -> Any:
        """A value that has no reason to be long, read from a piece of the text so that its cost has a limit."""
        await self._fill(NESTED_CELL_CHARS)
        window = self._text[self._at:self._at + NESTED_CELL_CHARS]
        try:
            value, end = self._json.raw_decode(window)
        except RecursionError as error:
            raise BackupError("The file is damaged or isn't in the backup format.") from error
        except ValueError as error:
            if len(window) >= NESTED_CELL_CHARS:
                raise BackupError("A value in this file is too large to read.") from error
            if self._ended and getattr(error, "pos", 0) >= len(window) - 16:
                raise BackupError("The file ends early. It may not have finished downloading.") from error
            raise BackupError("The file is damaged or isn't in the backup format.") from error
        self._at += end
        self.last_chars = end
        return value

    async def at_end(self) -> bool:
        try:
            await self.peek()
        except BackupError:
            return True
        return False


async def read_events(blocks: AsyncIterator[bytes]) -> AsyncIterator[tuple]:
    """Walks a backup file: ("meta", key, value), ("settings", dict), ("table", key, columns), ("rows", key, rows), ("end", key)."""
    stream = JsonStream(blocks)
    await stream.expect("{")
    first = True
    header = 0
    settings_seen = False
    while True:
        if await stream.peek() == "}":
            stream.skip()
            break
        if not first:
            await stream.expect(",")
        first = False
        name = await stream.value(MAX_HEADER_CHARS)
        await stream.expect(":")
        if name == "tables":
            async for event in _read_tables(stream):
                yield event
        elif name == "settings":
            if settings_seen:  # settings are the one large value read whole, so a file gets to do that once
                raise BackupError("This file isn't in the backup format.")
            settings_seen = True
            yield ("settings", await stream.value())
        else:
            value = await stream.value(MAX_HEADER_CHARS)
            header += stream.last_chars
            if header > MAX_HEADER_TOTAL:
                raise BackupError("This file isn't in the backup format.")
            yield ("meta", name, value)
    if not await stream.at_end():
        raise BackupError("There's something after the end of the backup in this file.")


async def _read_tables(stream: JsonStream) -> AsyncIterator[tuple]:
    await stream.expect("{")
    first = True
    while True:
        if await stream.peek() == "}":
            stream.skip()
            return
        if not first:
            await stream.expect(",")
        first = False
        key = await stream.value(MAX_HEADER_CHARS)
        await stream.expect(":")
        await stream.expect("{")
        columns = None
        inner_first = True
        while True:
            if await stream.peek() == "}":
                stream.skip()
                break
            if not inner_first:
                await stream.expect(",")
            inner_first = False
            part = await stream.value(MAX_HEADER_CHARS)
            await stream.expect(":")
            if part != "rows":
                content = await stream.value(MAX_HEADER_CHARS)
                if part == "columns":
                    columns = content
                continue
            if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
                raise BackupError("This file lists a table's rows before its columns.")
            yield ("table", key, columns)
            await stream.expect("[")
            batch: list = []
            batch_chars = 0
            batch_cells = 0
            row_first = True
            while True:
                if await stream.peek() == "]":
                    stream.skip()
                    break
                if not row_first:
                    await stream.expect(",")
                row_first = False
                batch.append(await stream.row())
                batch_chars += stream.last_chars
                batch_cells += stream.last_cells
                if len(batch) >= BATCH_ROWS or batch_chars >= BATCH_CHARS or batch_cells >= BATCH_CELLS:
                    yield ("rows", key, batch)
                    batch = []
                    batch_chars = 0
                    batch_cells = 0
                    await asyncio.sleep(0)
            if batch:
                yield ("rows", key, batch)
            yield ("end", key)


async def inflate(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Hands bytes through, unpacking them first when they start like gzip."""
    decoder: Any = None
    total = 0
    async for chunk in chunks:
        if decoder is None:
            decoder = zlib.decompressobj(31) if chunk[:2] == b"\x1f\x8b" else False
        if decoder is False:
            total += len(chunk)
            if total > MAX_INFLATED_BYTES:
                raise BackupError("This file is too large.")
            yield chunk
            continue
        data = chunk
        while data and not decoder.eof:
            try:
                out = await asyncio.to_thread(decoder.decompress, data, INFLATE_BLOCK)
            except zlib.error as error:
                raise BackupError("The file is damaged. It didn't unpack.") from error
            data = decoder.unconsumed_tail
            total += len(out)
            if total > MAX_INFLATED_BYTES:
                raise BackupError("This file unpacks to more than can be restored.")
            if out:
                yield out
    if decoder not in (None, False) and not decoder.eof:
        raise BackupError("The file ends early. It may not have finished downloading.")


# ---- a job that takes a while ----

@dataclass
class Job:
    kind: str
    stage: str = ""
    done: int = 0
    total: int = 0
    running: bool = True
    error: str | None = None
    result: Any = None
    task: asyncio.Task | None = None
    stopped: bool = False

    def state(self) -> dict:
        return {"kind": self.kind, "stage": self.stage, "done": self.done, "total": self.total, "running": self.running, "error": self.error, "result": self.result}


@dataclass
class Staged:
    """A file waiting for someone to look at its preview and decide."""
    token: str
    path: Path
    owned: bool  # an upload goes away after use. A snapshot stays.
    label: str
    created_at: float = field(default_factory=time.time)
    tables: set[str] = field(default_factory=set)  # what the preview found in the file


@dataclass
class RestoreCounts:
    added: int = 0
    already_here: int = 0
    removed: int = 0
    renumbered: int = 0
    dropped: int = 0


def _chunks_of(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


class Backup(Module):
    key = "backup"
    category = "Config"
    schema = Schema({
        "enabled": Field(True, boolean),
        "interval_hours": Field(24, choice(6, 12, 24, 48, 72, 168)),
        "keep": Field(14, integer(1, 100)),
        "include_messages": Field(False, boolean),
    })
    migrations = [
        """
        CREATE TABLE backup_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            file TEXT NOT NULL,
            created_at REAL NOT NULL,
            created_by TEXT,
            size INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            includes_messages INTEGER NOT NULL
        );
        CREATE INDEX backup_snapshots_guild ON backup_snapshots (guild_id, created_at);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._jobs: dict[int, Job] = {}
        self._staged: dict[int, Staged] = {}
        self._sweep_task: asyncio.Task | None = None
        self._job_gate = asyncio.Semaphore(MAX_JOBS_AT_ONCE)
        self._uploading: set[int] = set()
        self._downloads = 0
        self._exports: dict[int, tuple[str, Path, float]] = {}

    async def on_load(self) -> None:
        self.bot.timers.handler(SWEEP_KIND, self._sweep_due)
        if await self.bot.timers.get_key(SWEEP_KIND, "all") is None:
            await self.bot.timers.schedule(SWEEP_KIND, time.time() + FIRST_SWEEP_SECONDS, {}, key="all")
        await self._report_cut_off_restores()
        await self._tidy_files()

    def cog_unload(self) -> None:
        for job in self._jobs.values():
            if job.running and job.task is not None:
                job.task.cancel()
        if self._sweep_task is not None:
            self._sweep_task.cancel()

    # ---- places on disk ----

    @property
    def _root(self) -> Path:
        return Path(self.db.path).resolve().parent / "backups"

    def _snapshot_path(self, guild_id: int, name: str) -> Path:
        return self._root / str(guild_id) / name

    def _require_room(self) -> None:
        """A full disk stops the main database's writes for every server, so backup work stops first."""
        if shutil.disk_usage(Path(self.db.path).resolve().parent).free < MIN_FREE_BYTES:
            raise ApiError(507, "The bot's disk is nearly full, so this can't go on. Delete some snapshots, or ask whoever runs the bot to free up space.")

    async def _tidy_files(self) -> None:
        """A crash can leave a half-written file behind, and a waiting upload doesn't outlive a restart. Nothing else here is safe to delete."""
        root = self._root
        if not root.exists():
            return
        known = {(row["guild_id"], row["file"]) for row in await self.db.all("SELECT guild_id, file FROM backup_snapshots")}
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            for path in list(folder.iterdir()):
                whole = path.name.removesuffix("-wal").removesuffix("-shm")
                if whole.endswith(".part") or whole.startswith(("upload-", "export-")):
                    self._remove_file(path)
                elif whole == path.name and folder.name.isdigit() and (int(folder.name), path.name) not in known:
                    await self._adopt_snapshot(int(folder.name), path)

    async def _adopt_snapshot(self, guild_id: int, path: Path) -> None:
        """A whole snapshot with no row is what starting on an older copy of the database looks like. It may be the newest copy of this server there is, so it goes back on the list."""
        meta: dict = {}
        try:
            async with self._events_of(path) as events:
                async for event in events:
                    if event[0] != "meta":
                        break
                    meta[event[1]] = event[2]
        except (BackupError, DatabaseKeyError, cipher.DatabaseError) as error:
            log.warning("Left %s alone, because it can't be read as a snapshot: %s", path, error)
            return
        if meta.get("format") != FORMAT or str(meta.get("guild_id")) != str(guild_id):
            log.warning("Left %s alone, because it isn't a snapshot of the server its folder names.", path)
            return
        counts = meta.get("row_counts") if isinstance(meta.get("row_counts"), dict) else {}
        made = meta.get("created_at") if isinstance(meta.get("created_at"), (int, float)) else path.stat().st_mtime
        await self.db.insert(
            "INSERT INTO backup_snapshots (guild_id, kind, file, created_at, created_by, size, row_count, includes_messages) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, KIND_MANUAL, path.name, made, "Found after a recovery", path.stat().st_size, sum(v for v in counts.values() if isinstance(v, int)), int(bool(meta.get("includes_messages")))),
        )

    @staticmethod
    def _remove_file(path: Path) -> None:
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove %s", candidate)

    async def _store_stream(self, path: Path, blocks: AsyncIterator[bytes], *, limit: int | None = None) -> int:
        """Write a stream of bytes into an encrypted file. The name it goes to appears only once the file is whole."""
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        self._remove_file(partial)
        store = self.db.sibling(partial)
        await store.open()
        total = 0
        try:
            await store.run("CREATE TABLE chunks (seq INTEGER PRIMARY KEY, data BLOB NOT NULL)")
            sequence = 0
            pending = bytearray()
            async for block in blocks:
                total += len(block)
                if limit is not None and total > limit:
                    raise ApiError(413, "That file is too large to restore.")
                self._require_room()
                pending += block
                while len(pending) >= CHUNK_BYTES:
                    await store.run("INSERT INTO chunks (seq, data) VALUES (?, ?)", (sequence, bytes(pending[:CHUNK_BYTES])))
                    del pending[:CHUNK_BYTES]
                    sequence += 1
            if pending:
                await store.run("INSERT INTO chunks (seq, data) VALUES (?, ?)", (sequence, bytes(pending)))
        except BaseException:
            await store.close()
            self._remove_file(partial)
            raise
        await store.close()
        os.replace(partial, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return total

    @contextlib.asynccontextmanager
    async def _events_of(self, path: Path) -> AsyncIterator[AsyncIterator[tuple]]:
        """A stored file's contents as events. Closes every layer when done, because an open file can't be removed on some systems."""
        stored = self._read_stored(path)
        unpacked = inflate(stored)
        events = read_events(unpacked)
        try:
            yield events
        finally:
            await events.aclose()
            await unpacked.aclose()
            await stored.aclose()

    async def _read_stored(self, path: Path) -> AsyncIterator[bytes]:
        if not path.exists():
            raise BackupError("That file is gone from the server.")
        store = self.db.sibling(path)
        await store.open()
        try:
            sequence = -1
            while True:
                row = await store.one("SELECT seq, data FROM chunks WHERE seq > ? ORDER BY seq LIMIT 1", (sequence,))
                if row is None:
                    return
                sequence = row["seq"]
                yield bytes(row["data"])
        finally:
            await store.close()

    # ---- secrets ----

    def _seal(self, guild_id: int, where: str, value: Any) -> dict:
        nonce = secrets.token_bytes(12)
        sealed = AESGCM(self.db.derived_key(SEAL_PURPOSE)).encrypt(nonce, json.dumps(value).encode(), f"{guild_id}|{where}".encode())
        return {"$sealed": base64.b64encode(nonce + sealed).decode("ascii")}

    def _unseal(self, guild_id: int, where: str, value: dict) -> Any:
        """The sealed value, or BadCell when another install sealed it or it was moved from elsewhere."""
        try:
            raw = base64.b64decode(value["$sealed"], validate=True)
            plain = AESGCM(self.db.derived_key(SEAL_PURPOSE)).decrypt(raw[:12], raw[12:], f"{guild_id}|{where}".encode())
            return json.loads(plain)
        except (InvalidTag, ValueError, TypeError) as error:
            raise BadCell("A secret can't be read") from error

    @staticmethod
    def _is_sealed(value: Any) -> bool:
        return isinstance(value, dict) and set(value) == {"$sealed"} and isinstance(value["$sealed"], str)

    def _seal_settings(self, guild_id: int, schema: Schema, data: dict, where: str) -> dict:
        out = dict(data)
        for name, spec in schema.fields.items():
            if isinstance(spec, Schema):
                if isinstance(out.get(name), dict):
                    out[name] = self._seal_settings(guild_id, spec, out[name], f"{where}.{name}")
            elif spec.secret and out.get(name):
                out[name] = self._seal(guild_id, f"{where}.{name}", out[name])
        return out

    def _open_settings(self, guild_id: int, schema: Schema, data: dict, where: str, warnings: list[str]) -> dict:
        """Sealed values opened. One that can't be opened becomes the mask, which keeps whatever this server has stored now."""
        out = dict(data)
        for name, spec in schema.fields.items():
            if isinstance(spec, Schema):
                if isinstance(out.get(name), dict):
                    out[name] = self._open_settings(guild_id, spec, out[name], f"{where}.{name}", warnings)
            elif spec.secret and name in out:
                if self._is_sealed(out[name]):
                    try:
                        out[name] = self._unseal(guild_id, f"{where}.{name}", out[name])
                    except BadCell:
                        out[name] = SECRET_MASK
                        warnings.append(f"The file's {where}.{name} can't be read here, so the one stored now stays. A different install or master key sealed it.")
                elif out[name]:
                    out[name] = out[name] if out[name] == SECRET_MASK else SECRET_MASK
                    warnings.append(f"The file's {where}.{name} isn't sealed, so it was ignored and the one stored now stays.")
        return out

    def _activity_out(self, guild_id: int, columns: list[str], row: list) -> list:
        """The activity log holds whole settings forms for undo, so secrets in them get the same seal as the settings do."""
        module = row[columns.index("module")]
        schema = self.bot.settings.schemas.get(module)
        if schema is None or not self._has_secret(schema):
            return row
        out = list(row)
        for name in ACTIVITY_JSON_COLUMNS:
            at = columns.index(name)
            if out[at]:
                try:
                    out[at] = json.dumps(self._seal_settings(guild_id, schema, json.loads(out[at]), f"activity.{module}"))
                except (ValueError, TypeError, AttributeError):
                    out[at] = None
        return out

    def _activity_in(self, guild_id: int, columns: list[str], row: list) -> list:
        at_module = columns.index("module") if "module" in columns else None
        schema = self.bot.settings.schemas.get(row[at_module]) if at_module is not None else None
        out = list(row)
        for name in ACTIVITY_JSON_COLUMNS:
            if name not in columns:
                continue
            at = columns.index(name)
            if not isinstance(out[at], str) or not out[at]:
                continue
            if schema is None or not self._has_secret(schema):
                continue
            try:
                ignored: list[str] = []
                opened = self._open_settings(guild_id, schema, json.loads(out[at]), f"activity.{row[at_module]}", ignored)
                # A form with a secret that can't be opened can't be undone to safely, so that row loses its stored form.
                out[at] = None if ignored else json.dumps(opened)
            except (ValueError, TypeError, AttributeError):
                out[at] = None
        return out

    def _row_mark(self, guild_id: int, table_key: str, cells: list) -> str:
        """Proof that this install wrote these cells for this server, in this table. Made over the cells as the file holds them."""
        message = json.dumps([str(guild_id), table_key, cells])
        return hmac.new(self.db.derived_key(MARK_PURPOSE), message.encode(), hashlib.sha256).hexdigest()

    @classmethod
    def _has_secret(cls, schema: Schema) -> bool:
        return any(cls._has_secret(spec) if isinstance(spec, Schema) else spec.secret for spec in schema.fields.values())

    # ---- writing a file ----

    async def catalogs(self, *views: tuple[str, Database]) -> Catalog:
        owners = table_owners(self.bot)
        children = child_links(self.bot)
        merged = Catalog({}, [])
        for label, view in views:
            found = await read_catalog(view, label, owners, children)
            merged.tables.update(found.tables)
            merged.left_out += found.left_out
        return merged

    async def export_stream(self, guild_id: int, *, include_messages: bool, counts: dict | None = None) -> AsyncIterator[bytes]:
        """The whole file as blocks of bytes. Both databases are read from one unchanging view, so the file is consistent in itself."""
        async with self.db.reader() as main_view:
            if not include_messages:
                async with contextlib.aclosing(self._write_document(guild_id, [("main", main_view)], counts)) as blocks:
                    async for block in blocks:
                        yield block
                return
            async with self.message_db.reader() as message_view:
                async with contextlib.aclosing(self._write_document(guild_id, [("main", main_view), ("messages", message_view)], counts)) as blocks:
                    async for block in blocks:
                        yield block

    async def _write_document(self, guild_id: int, views: list[tuple[str, Database]], counts: dict | None) -> AsyncIterator[bytes]:
        catalog = await self.catalogs(*views)
        view_of = dict(views)
        row_counts: dict[str, int] = {}
        for table in catalog.ordered():
            row_counts[table.key] = await view_of[table.database].scalar(f"SELECT COUNT(*) FROM {quote(table.name)} WHERE {table.where}", (guild_id,))
        if counts is not None:
            counts.update(row_counts)
        info = self.bot.ops.describe_guild(guild_id) or {}
        header = {
            "format": FORMAT,
            "version": VERSION,
            "guild_id": str(guild_id),
            "guild_name": info.get("name"),
            "created_at": time.time(),
            "app_version": __version__,
            "includes_messages": len(views) > 1,
            "row_counts": row_counts,
            "left_out": catalog.left_out,
        }
        parts = [dump_json(name) + b":" + dump_json(value) for name, value in header.items()]
        yield b"{" + b",\n".join(parts) + b",\n"
        yield b'"settings":{\n'
        for index, module in enumerate(sorted(self.bot.settings.schemas)):
            schema = self.bot.settings.schemas[module]
            stored = self._seal_settings(guild_id, schema, self.bot.settings.get(guild_id, module), module)
            yield (b",\n" if index else b"") + dump_json(module) + b":" + dump_json(safe_json(stored))
        yield b'\n},\n"tables":{\n'
        skipped: dict[str, int] = {}
        for index, table in enumerate(catalog.ordered()):
            columns = [*table.columns, MARK_COLUMN] if table.key in MARKED_TABLES else table.columns
            head = dump_json(table.key) + b':{"columns":' + dump_json(columns) + b',"rows":[\n'
            yield (b",\n" if index else b"") + head
            async with contextlib.aclosing(self._write_rows(guild_id, view_of[table.database], table, skipped)) as blocks:
                async for block in blocks:
                    yield block
            yield b"\n]}"
        yield b'\n},\n"rows_left_out":' + dump_json(skipped) + b"}\n"

    async def _write_rows(self, guild_id: int, view: Database, table: Table, skipped: dict[str, int]) -> AsyncIterator[bytes]:
        names = ", ".join(quote(c) for c in table.columns)
        affinity = [table.affinity[c] for c in table.columns]
        secret_at = [i for i, c in enumerate(table.columns) if _SECRET_COLUMN.search(c)]
        marked_at = [table.columns.index(name) for name in MARKED_TABLES.get(table.key, ())]
        cursor = await view.conn.execute(f"SELECT {names} FROM {quote(table.name)} WHERE {table.where}", (guild_id,))
        first = True
        try:
            while True:
                rows = await cursor.fetchmany(BATCH_ROWS)
                if not rows:
                    return
                lines = []
                for row in rows:
                    values = list(row)
                    if table.key == "main.activity":
                        values = self._activity_out(guild_id, table.columns, values)
                    cells = [encode_cell(value, kind) for value, kind in zip(values, affinity)]
                    for at in secret_at:
                        if isinstance(cells[at], str) and cells[at]:
                            cells[at] = self._seal(guild_id, f"{table.key}.{table.columns[at]}", cells[at])
                    if marked_at:
                        cells.append(self._row_mark(guild_id, table.key, [cells[at] for at in marked_at]))
                    line = dump_json(cells)
                    if len(line) > MAX_STRING_CHARS:  # a file never holds what it couldn't read back, and one such row would refuse the whole file
                        log.warning("Left a row of %s out of a backup of %s, at %d bytes.", table.key, guild_id, len(line))
                        skipped[table.key] = skipped.get(table.key, 0) + 1
                        continue
                    lines.append(line)
                if lines:
                    yield (b"" if first else b",\n") + b",\n".join(lines)
                    first = False
        finally:
            await cursor.close()

    @staticmethod
    async def _gzip(blocks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        packer = zlib.compressobj(6, zlib.DEFLATED, 31)
        async for block in blocks:
            out = await asyncio.to_thread(packer.compress, block)
            if out:
                yield out
        yield packer.flush()

    # ---- checking what a file holds ----

    def _require_usable(self, meta: dict, guild_id: int) -> None:
        if meta.get("format") != FORMAT:
            raise BackupError("This isn't a backup file from this bot.")
        version = meta.get("version")
        if not isinstance(version, int) or version > VERSION:
            raise BackupError("A newer version of this bot made that file. Update this one first.")
        if str(meta.get("guild_id")) != str(guild_id):
            name = meta.get("guild_name") or meta.get("guild_id")
            raise BackupError(f"This file holds the data of {name}, and a backup can only go back to the server it came from.")

    def _channel_elsewhere(self, guild_id: int, value: Any) -> bool:
        if isinstance(value, str):
            try:
                value = int(value.strip())  # whatever int() can read, because code that uses this value later can read it too
            except ValueError:
                return False
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        channel = self.bot.get_channel(value)
        guild = getattr(channel, "guild", None)
        return guild is not None and guild.id != guild_id

    def _reaches_elsewhere(self, guild_id: int, name: str, value: Any) -> bool:
        """Whether a value names another server or a channel in one. Plenty of code finds channels across all servers, so this has to stop at the door."""
        if isinstance(value, dict):
            return any(self._reaches_elsewhere(guild_id, str(key), item) for key, item in value.items())
        if isinstance(value, list):
            return any(self._reaches_elsewhere(guild_id, name, item) for item in value)
        lowered = name.lower()
        if lowered == "guild_id":
            return value is not None and str(value) != str(guild_id)
        return ("channel" in lowered or "thread" in lowered) and self._channel_elsewhere(guild_id, value)

    def _row_reaches_elsewhere(self, guild_id: int, columns: list[str], values: list, *, read_json: bool) -> bool:
        for name, value in zip(columns, values):
            if read_json and isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
                if len(value) > MAX_JSON_CHECK_CHARS:
                    continue
                try:
                    if self._reaches_elsewhere(guild_id, name, json.loads(value)):
                        return True
                except RecursionError:  # nothing this bot writes nests that deep
                    return True
                except ValueError:
                    pass
            elif name != "guild_id" and self._reaches_elsewhere(guild_id, name, value):
                return True
        return False

    def _clean_settings(self, guild_id: int, module: str, raw: Any, warnings: list[str]) -> dict | None:
        """The settings this file would leave in place, checked field by field. A bad field keeps its current value. Fields it doesn't mention stay as they are."""
        schema = self.bot.settings.schemas.get(module)
        if schema is None:
            warnings.append(f"This bot has no settings called {module} anymore, so the file's copy was ignored.")
            return None
        if not isinstance(raw, dict):
            warnings.append(f"The file's {module} settings aren't readable, so they were ignored.")
            return None
        opened = self._open_settings(guild_id, schema, raw, module, warnings)
        return self._clean_fields(guild_id, schema, opened, self.bot.settings.get(guild_id, module), module, warnings)

    def _clean_fields(self, guild_id: int, schema: Schema, data: dict, current: dict, where: str, warnings: list[str]) -> dict:
        out = dict(current)
        for name, value in data.items():
            label = f"{where}.{name}"
            spec = schema.fields.get(name)
            if spec is None:
                warnings.append(f"{label} no longer exists, so it was ignored.")
                continue
            if isinstance(spec, Schema):
                if isinstance(value, dict):
                    out[name] = self._clean_fields(guild_id, spec, value, current.get(name) or {}, label, warnings)
                else:
                    warnings.append(f"{label} isn't readable, so it keeps its current value.")
                continue
            if safe_json(current.get(name)) == value:  # unchanged, so it has nothing to prove
                continue
            if self._reaches_elsewhere(guild_id, name, value):
                warnings.append(f"{label} names a channel in another server, so it keeps its current value.")
                continue
            try:
                cleaned = schema.clean({name: value}, base=out)
            except ValidationError as error:
                warnings.append(f"{label} was refused ({next(iter(error.fields.values()))}), so it keeps its current value.")
                continue
            # The same check the dashboard makes on what the schema keeps, whatever the field is called.
            if field_naming_another_server(self.bot, guild_id, {name: cleaned[name]}) is not None:
                warnings.append(f"{label} names a channel or role in another server, so it keeps its current value.")
                continue
            out[name] = cleaned[name]
        return out

    def _settings_item(self, module: str, before: dict, after: dict) -> dict:
        schema = self.bot.settings.schemas[module]
        shown_before, shown_after = schema.masked(before), schema.masked(after)

        def short(value: Any) -> str:
            text = json.dumps(safe_json(value), ensure_ascii=False)
            return text if len(text) <= CHANGE_PREVIEW_CHARS else text[:CHANGE_PREVIEW_CHARS - 3] + "..."

        changes = []
        for name in schema.fields:
            if before.get(name) != after.get(name):
                changes.append({"field": name, "before": short(shown_before.get(name)), "after": short(shown_after.get(name))})
        return {"key": f"settings:{module}", "module": module, "changes": changes}

    # ---- the preview ----

    async def _build_preview(self, guild_id: int, staged: Staged, job: Job) -> dict:
        catalog = await self.catalogs(("main", self.db), ("messages", self.message_db))
        meta: dict = {}
        warnings: list[str] = []
        settings: list[dict] = []
        items: dict[str, dict] = {}
        parents: dict[tuple[str, str], set] = {}
        seen_tables: set[str] = set()
        problems: list[str] = []
        unusable: set[str] = set()
        current: dict | None = None
        current_table: Table | None = None
        cleaner: RowCleaner | None = None
        budget = [MAX_RESTORE_ROWS]
        job.stage = "Reading the file"
        async with self._events_of(staged.path) as events:
            async for event in events:
                kind = event[0]
                if kind == "meta":
                    meta[event[1]] = event[2]
                    if event[1] == "row_counts" and isinstance(event[2], dict):
                        job.total = sum(v for v in event[2].values() if isinstance(v, int))
                elif kind == "settings":
                    self._require_usable(meta, guild_id)
                    if not isinstance(event[1], dict):
                        raise BackupError("The file's settings aren't readable.")
                    for module, raw in sorted(event[1].items()):
                        after = self._clean_settings(guild_id, module, raw, warnings)
                        if after is not None:
                            settings.append(self._settings_item(module, self.bot.settings.get(guild_id, module), after))
                elif kind == "table":
                    self._require_usable(meta, guild_id)
                    key, columns = event[1], event[2]
                    current_table = catalog.tables.get(key)
                    cleaner = None
                    if current_table is None:
                        if key not in seen_tables and len(seen_tables) < MAX_WARNINGS:
                            problems.append(f"The file has a table this bot doesn't know, {key}, so it was ignored.")
                            seen_tables.add(key)
                        continue
                    staged.tables.add(key)
                    cleaner = RowCleaner(self, guild_id, current_table, columns, parents, catalog, budget=budget)
                    current, total_now = await self._current_rows(guild_id, current_table, cleaner.columns)
                    items[key] = self._table_item(current_table, total_now, compared=current is not None)
                    if current_table.name == "timers":
                        cleaner.scheduled_now = {(row["kind"], row["key"]) for row in await self.db.all("SELECT kind, key FROM timers WHERE key IS NOT NULL")}
                    if cleaner.missing_required:
                        unusable.add(key)
                        problems.append(f"{key} has a required column the file doesn't hold, {cleaner.missing_required[0]}, so it stays as it is here.")
                    elif cleaner.unprovable:
                        unusable.add(key)
                        problems.append(f"{key}: this file has no proof that this install wrote these rows, as with a file from an older version. They stay out, and what's here stays as it is.")
                elif kind == "rows":
                    job.done += len(event[2])
                    if cleaner is None:
                        continue
                    self._compare_rows(guild_id, current_table, cleaner, event[2], current, items[event[1]])
                elif kind == "end":
                    if cleaner is not None:
                        item = items[event[1]]
                        if current is not None and not current_table.merges:
                            item["removed"] = len(current)
                        item["dropped"] = cleaner.dropped + cleaner.over_limit
                        if cleaner.over_limit:
                            problems.append(f"{event[1]}: {cleaner.over_limit} rows are past what a server can hold, so they stay out.")
                        item["foreign"] = cleaner.foreign + cleaner.unmarked
                        cleaner.finish()
                        if cleaner.untrusted:
                            problems.append(f"{cleaner.untrusted} {current_table.name} rows in this file come back without trust, because nothing proves this install wrote them. Trust them again on their page after reading them.")
                        if cleaner.unmarked and not cleaner.unprovable:
                            what ={"timers": "scheduled jobs", "activity": "activity rows"}.get(current_table.name, f"{current_table.name} rows")
                            problems.append(f"{cleaner.unmarked} {what} in this file weren't made by this install or were changed, so they stay out.")
                        if cleaner.extra_columns:
                            problems.append(f"{event[1]}: the column {cleaner.extra_columns[0]} no longer exists, so its values were ignored.")
                    current = cleaner = current_table = None
        self._require_usable(meta, guild_id)
        self._leave_out_partial_units(catalog, staged, items, unusable, problems)
        if not items and not settings and not problems:
            raise BackupError("This file holds nothing to restore.")
        stated = meta.get("row_counts") if isinstance(meta.get("row_counts"), dict) else {}
        skipped = meta.get("rows_left_out") if isinstance(meta.get("rows_left_out"), dict) else {}
        for key, count in skipped.items():
            if isinstance(count, int) and count > 0:
                problems.append(f"{key}: {count} rows were too large to go in this file when it was made, so they aren't in it.")
        for key, item in items.items():
            held = stated.get(key)
            if isinstance(held, int) and isinstance(skipped.get(key), int):
                held -= skipped[key]
            if isinstance(held, int) and held != item["in_file"]:
                stated = {**stated, key: held}
                problems.append(f"{key}: the file says it holds {stated[key]} rows and it holds {item['in_file']}. Someone may have edited it.")
        for item in items.values():
            if item.get("past_due"):
                kinds = ", ".join(sorted(item["past_due_kinds"])[:20])
                problems.append(f"{item['past_due']} scheduled jobs in this file are already past due, and they run right after the restore: {kinds}.")
            if item.get("past_role_changes"):
                problems.append(f"{item['past_role_changes']} scheduled changes in this file, such as a role coming off or a ban lifting, are already past due, so they stay out.")
        return {
            "token": staged.token,
            "label": staged.label,
            "file": {
                "guild_id": str(meta.get("guild_id")), "guild_name": meta.get("guild_name"), "created_at": meta.get("created_at"),
                "app_version": meta.get("app_version"), "includes_messages": bool(meta.get("includes_messages")),
                "left_out": meta.get("left_out") if isinstance(meta.get("left_out"), list) else [],
            },
            "settings": settings,
            "tables": [items[key] for key in sorted(items, key=lambda k: (items[k]["owner"], k))],
            "warnings": list(dict.fromkeys([*warnings, *problems]))[:MAX_WARNINGS],
        }

    @staticmethod
    def _leave_out_partial_units(catalog: Catalog, staged: Staged, items: dict, unusable: set[str], problems: list[str]) -> None:
        """Tables tied by keys come back together or not at all, because restoring a parent alone takes its children with it, and restoring a child alone loses its rows."""
        units: dict[str, list[Table]] = {}
        for table in catalog.tables.values():
            if not table.merges:
                units.setdefault(table.group, []).append(table)
        for members in units.values():
            held = [table for table in members if table.key in staged.tables and table.key not in unusable]
            whole = len(held) == len(members)
            for table in members:
                if not whole and table.key in staged.tables:
                    staged.tables.discard(table.key)
                    items.pop(table.key, None)
            if held and not whole:
                missing = ", ".join(sorted(table.name for table in members if table not in held))
                kept = ", ".join(sorted(table.name for table in held))
                problems.append(f"The file holds {kept} without {missing}, which go together, so all of them stay as they are here.")

    @staticmethod
    def _table_item(table: Table, now: int, *, compared: bool) -> dict:
        return {
            "key": f"table:{table.key}", "table": table.name, "database": table.database, "owner": table.owner, "group": f"table:{table.group}",
            "mode": "merge" if table.merges else "replace", "now": now, "in_file": 0, "added": 0, "changed": 0, "removed": 0, "same": 0,
            "dropped": 0, "foreign": 0, "compared": compared, "past_due": 0, "past_due_kinds": {}, "past_role_changes": 0,
        }

    async def _current_rows(self, guild_id: int, table: Table, columns: list[str]) -> tuple[dict | None, int]:
        """What's here now, as a compact map for comparing, unless the table is too big for that."""
        database = self.db if table.database == "main" else self.message_db
        count = await database.scalar(f"SELECT COUNT(*) FROM {quote(table.name)} WHERE {table.where}", (guild_id,))
        if count > DIFF_ROW_LIMIT or not columns:
            return None, count
        key_at = [columns.index(c) for c in table.primary_key if c in columns] if table.primary_key else []
        names = ", ".join(quote(c) for c in columns)
        current: dict = {}
        async with self.db.reader() if table.database == "main" else self.message_db.reader() as view:
            cursor = await view.conn.execute(f"SELECT {names} FROM {quote(table.name)} WHERE {table.where}", (guild_id,))
            while True:
                rows = await cursor.fetchmany(BATCH_ROWS)
                if not rows:
                    break
                for row in rows:
                    values = tuple(row)
                    identity = tuple(values[at] for at in key_at) if key_at else values
                    current[identity] = hash(values)
            await cursor.close()
        return current, count

    def _compare_rows(self, guild_id: int, table: Table, cleaner: "RowCleaner", rows: list, current: dict | None, item: dict) -> None:
        columns = cleaner.columns
        key_at = [columns.index(c) for c in table.primary_key if c in columns] if table.primary_key else []
        due_at = columns.index("due_at") if table.name == "timers" and "due_at" in columns else None
        now = time.time()
        at_kind = columns.index("kind") if due_at is not None and "kind" in columns else None
        at_key = columns.index("key") if due_at is not None and "key" in columns else None
        for raw in rows:
            item["in_file"] += 1
            values = cleaner.clean(raw)
            if values is None:
                continue
            cleaner.remember(values)
            if at_kind is not None and at_key is not None and (values[at_kind], values[at_key]) in cleaner.scheduled_now:
                item["same"] += 1  # the job scheduled now is the one that stays
                continue
            if due_at is not None and isinstance(values[due_at], (int, float)) and values[due_at] <= now:
                if at_kind is not None and self._past_role_change(values[at_kind], values[due_at], now):
                    item["past_role_changes"] += 1
                else:
                    item["past_due"] += 1
                    if at_kind is not None:
                        item["past_due_kinds"][str(values[at_kind])] = True
            if current is None:
                continue
            frozen = tuple(values)
            identity = tuple(frozen[at] for at in key_at) if key_at else frozen
            known = current.pop(identity, None)
            if known is None:
                item["added"] += 1
            elif known == hash(frozen):
                item["same"] += 1
            else:
                item["changed"] += 1

    # ---- restoring ----

    async def _restore(self, guild_id: int, staged: Staged, chosen: set[str], actor: Any, job: Job) -> dict:
        job.stage = "Saving the current state first"
        before = await self._write_snapshot(guild_id, kind=KIND_BEFORE_IMPORT, actor_name=getattr(actor, "name", None), include_messages=False)
        job.done, job.total = 0, 0
        catalog = await self.catalogs(("main", self.db), ("messages", self.message_db))
        wanted_groups = {catalog.tables[key[len("table:"):]].group for key in chosen if key.startswith("table:") and key[len("table:"):] in catalog.tables}
        wanted = {key for key, table in catalog.tables.items() if table.group in wanted_groups}
        report: dict[str, RestoreCounts] = {}
        warnings: list[str] = []
        changed_modules: list[str] = []

        job.stage = "Reading the file"
        # A table the file doesn't hold stays as it is here, for example one a newer module added after the file was made.
        restoring = [t for t in catalog.ordered("main") if t.key in wanted and t.key in staged.tables]
        holding = self._snapshot_path(guild_id, f"{uuid.uuid4().hex}.holding.part")
        try:
            settings_raw = await self._hold_rows(guild_id, staged, catalog, restoring, holding, report, job)
            job.stage = "Restoring tables"
            await self.bot.activity_log.record(guild_id, actor, "backup.import.started", detail=staged.label)
            await self.db.run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (f"{RESTORING_KEY}{guild_id}", staged.label))
            # Nothing of this server's runs until its rows, settings and what modules hold in memory agree again.
            self.bot.timers.hold(guild_id)
            try:
                await self._tell_modules("before_restore", guild_id)
                try:
                    await self.db.exclusive(lambda connection: self._swap_rows(connection, guild_id, restoring, report), attach=(holding, HOLDING), cache_kib=SWAP_CACHE_KIB)
                except cipher.IntegrityError as error:
                    raise BackupError("Rows in this file point at rows it doesn't hold, so nothing was changed.") from error
                # Settings and what modules hold in memory follow at once, so a restart during the long read of the messages can't leave the tables and the settings from different times.
                job.stage = "Restoring settings"
                for module in sorted(settings_raw):
                    if f"settings:{module}" not in chosen:
                        continue
                    after = self._clean_settings(guild_id, module, settings_raw[module], warnings)
                    if after is None or after == self.bot.settings.get(guild_id, module):
                        continue
                    try:
                        await self.bot.settings.restore(guild_id, module, after, actor=actor)
                        changed_modules.append(module)
                    except Exception:  # the row is written before listeners run, so a listener's failure doesn't undo the restore
                        log.exception("A listener failed while restoring %s for %s", module, guild_id)
                        changed_modules.append(module)
                await self._tell_modules("on_guild_restored", guild_id)
                await self._restore_finished(guild_id)
            except Exception:  # a failed swap changed nothing, while a stop on the way out leaves the mark for the next start to find
                await self._restore_finished(guild_id)
                raise
            finally:
                self.bot.timers.release(guild_id)
                await self._tell_modules("after_restore", guild_id)
        finally:
            self._remove_file(holding)

        message_tables = {t.key for t in catalog.ordered("messages") if t.key in wanted and t.key in staged.tables}
        if message_tables:
            job.stage = "Adding messages and logs"
            await self._restore_pass(guild_id, staged, catalog, message_tables, self.message_db, report, job)

        job.stage = "Finishing up"
        added = sum(c.added for c in report.values())
        removed = sum(c.removed for c in report.values())
        await self.bot.activity_log.record(guild_id, actor, "backup.import", detail=f"{staged.label}: {added} rows put back, {removed} replaced, {len(changed_modules)} settings pages changed")
        return {
            "before_snapshot": before["id"],
            "settings": changed_modules,
            "tables": {key: vars(counts) for key, counts in report.items()},
            "warnings": list(dict.fromkeys(warnings)),
        }

    async def _restore_finished(self, guild_id: int) -> None:
        await self.db.run("DELETE FROM kv WHERE key = ?", (f"{RESTORING_KEY}{guild_id}",))

    async def _report_cut_off_restores(self) -> None:
        """A restore the last run didn't finish. Its tables may be from the file with the settings still from before, and the Activity page is where someone will look."""
        for row in await self.db.all("SELECT key, value FROM kv WHERE key LIKE ?", (f"{RESTORING_KEY}%",)):
            guild_id = row["key"].removeprefix(RESTORING_KEY)
            if guild_id.isdigit():
                await self.bot.activity_log.record(int(guild_id), None, "backup.import.interrupted", detail=f"{row['value']}: the bot stopped during this restore. Its tables may have come back without its settings. Restore the file again, or the snapshot taken before it.")
            await self.db.run("DELETE FROM kv WHERE key = ?", (row["key"],))

    async def _tell_modules(self, hook: str, guild_id: int) -> None:
        for module in self.bot.modules:
            try:
                await getattr(module, hook)(guild_id)
            except Exception:
                log.exception("%s failed at %s for %s", module.key, hook, guild_id)

    async def _hold_rows(self, guild_id: int, staged: Staged, catalog: Catalog, restoring: list[Table], path: Path, report: dict[str, RestoreCounts], job: Job) -> dict:
        """Check the file's rows into a holding file with the same tables. Reading a big file takes minutes, and this touches no live table and holds no lock that long. Returns the file's settings."""
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._remove_file(path)
        store = self.db.sibling(path)
        await store.open()
        try:
            await store.conn.execute("PRAGMA foreign_keys=OFF")  # a row's parent is checked when it goes live
            await store.conn.execute("PRAGMA synchronous=OFF")  # this file is thrown away if anything goes wrong
            created = {row["name"]: row["sql"] for row in await self.db.all("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
            for table in restoring:
                await store.run(created[table.name])
            return await self._restore_pass(guild_id, staged, catalog, {t.key for t in restoring}, store, report, job)
        finally:
            await store.close()

    def _swap_rows(self, connection: Any, guild_id: int, restoring: list[Table], report: dict[str, RestoreCounts]) -> None:
        """Replace the live rows with the held ones. This runs on the database's own thread inside one transaction, so nothing else in the bot reads or writes halfway through."""
        connection.execute("PRAGMA defer_foreign_keys = ON")
        for table in reversed(restoring):
            if not table.merges:
                report.setdefault(table.key, RestoreCounts()).removed = connection.execute(f"DELETE FROM main.{quote(table.name)} WHERE {table.where}", (guild_id,)).rowcount
        new_numbers: dict[str, str] = {}
        for table in restoring:
            self._swap_table(connection, guild_id, table, report.setdefault(table.key, RestoreCounts()), new_numbers)
        self._drop_strays(connection, guild_id, restoring, report)
        for table in restoring:
            if connection.execute(f"PRAGMA main.foreign_key_check({quote(table.name)})").fetchall():
                raise BackupError(f"Rows in {table.name} point at rows the file doesn't hold, so nothing was changed.")
        for name in new_numbers.values():
            connection.execute(f"DROP TABLE temp.{name}")

    @staticmethod
    def _drop_strays(connection: Any, guild_id: int, restoring: list[Table], report: dict[str, RestoreCounts]) -> None:
        """The last word on crossing servers: once the rows are in, a child in this server whose parent isn't in this server goes. Two file rows can share one parent number, and only one of them gets a new number when another server holds it."""
        by_name = {table.name: table for table in restoring}
        for table in restoring:
            for link in table.links:
                parent = by_name.get(link.parent)
                if parent is None or "guild_id" not in table.columns:
                    continue
                column = quote(link.column)
                strays = connection.execute(
                    f"DELETE FROM main.{quote(table.name)} WHERE {table.where} AND {column} IS NOT NULL AND {column} NOT IN (SELECT {quote(link.parent_column)} FROM main.{quote(parent.name)} WHERE {parent.where})",
                    (guild_id, guild_id),
                ).rowcount
                if strays > 0:
                    counts = report[table.key]
                    counts.added = max(counts.added - strays, 0)
                    counts.dropped += strays

    @staticmethod
    def _swap_table(connection: Any, guild_id: int, table: Table, counts: RestoreCounts, new_numbers: dict[str, str]) -> None:
        target, source = f"main.{quote(table.name)}", f"{HOLDING}.{quote(table.name)}"
        names = ", ".join(quote(c) for c in table.columns)
        held, counts.added = counts.added, 0

        # A row whose parent had to take a new number follows it.
        cells = []
        for column in table.columns:
            mapping = next((new_numbers[f"{table.database}.{link.parent}"] for link in table.links if link.column == column and f"{table.database}.{link.parent}" in new_numbers), None)
            cells.append(f"COALESCE((SELECT new FROM temp.{mapping} WHERE old = s.{quote(column)}), s.{quote(column)})" if mapping else f"s.{quote(column)}")
        picked = ", ".join(cells)

        only = ""
        if table.name == "timers":  # a timer with a key is one job however many rows mention it, and the one scheduled now is the one to keep
            only = f' AND (s."key" IS NULL OR NOT EXISTS (SELECT 1 FROM {target} m WHERE m."kind" = s."kind" AND m."key" = s."key"))'

        moved = 0
        if table.rowid_key:
            key = quote(table.rowid_key)
            holders = [row[0] for row in connection.execute(
                f"SELECT s.{key} FROM {source} s WHERE s.{key} IS NOT NULL{only} AND EXISTS (SELECT 1 FROM {target} WHERE {key} = s.{key} AND NOT COALESCE(({table.where}), 0))", (guild_id,),
            )]
            if holders:  # another server's rows hold these numbers
                mapping = f"restore_numbers_{len(new_numbers)}"
                connection.execute(f"CREATE TEMP TABLE {mapping} (old INTEGER PRIMARY KEY, new INTEGER NOT NULL)")
                new_numbers[f"{table.database}.{table.name}"] = mapping
                at = table.columns.index(table.rowid_key)
                marks = ", ".join("?" for _ in table.columns)
                for old in holders:
                    values = list(connection.execute(f"SELECT {picked} FROM {source} s WHERE s.{key} = ?", (old,)).fetchone())
                    values[at] = None
                    cursor = connection.execute(f"INSERT INTO {target} ({names}) VALUES ({marks}) ON CONFLICT DO NOTHING", values)
                    if cursor.rowcount > 0:
                        connection.execute(f"INSERT INTO temp.{mapping} (old, new) VALUES (?, ?)", (old, cursor.lastrowid))
                        moved += 1
                only += f" AND s.{key} NOT IN (SELECT old FROM temp.{mapping})"
        inserted = connection.execute(f"INSERT INTO {target} ({names}) SELECT {picked} FROM {source} s WHERE 1{only} ON CONFLICT DO NOTHING").rowcount
        inserted = max(inserted, 0)
        counts.added += inserted + moved
        counts.renumbered += moved
        counts.already_here += max(held - inserted - moved, 0)

    async def _restore_pass(self, guild_id: int, staged: Staged, catalog: Catalog, wanted: set[str], database: Database, report: dict[str, RestoreCounts], job: Job) -> dict:
        """Read the file once and write the rows of the chosen tables to this database, in small transactions so its own writers get a turn between them. Returns the file's settings."""
        settings_raw: dict = {}
        settings_seen = False
        parents: dict[tuple[str, str], set] = {}
        renumbered: dict[str, dict[Any, Any]] = {}
        meta: dict = {}
        cleaner: RowCleaner | None = None
        table: Table | None = None
        remaining = set(wanted)
        budget = [MAX_RESTORE_ROWS]
        async with self._events_of(staged.path) as events:
            async for event in events:
                kind = event[0]
                if kind == "meta":
                    meta[event[1]] = event[2]
                    if event[1] == "row_counts" and isinstance(event[2], dict):
                        job.total += sum(count for key, count in event[2].items() if key in wanted and isinstance(count, int))
                elif kind == "settings":
                    self._require_usable(meta, guild_id)
                    settings_raw = event[1] if isinstance(event[1], dict) else {}
                    settings_seen = True
                    if not remaining:  # only the settings were wanted from this read
                        break
                elif kind == "table":
                    self._require_usable(meta, guild_id)
                    table = catalog.tables.get(event[1]) if event[1] in wanted else None
                    cleaner = RowCleaner(self, guild_id, table, event[2], parents, catalog, renumbered, budget) if table else None
                elif kind == "rows" and cleaner is not None:
                    self._require_room()
                    counts = report.setdefault(table.key, RestoreCounts())
                    rows = []
                    for raw in event[2]:
                        values = cleaner.clean(raw)
                        if values is not None:
                            rows.append(values)
                    async with database.transaction():
                        await self._insert_rows(database, table, cleaner, rows, counts, renumbered)
                    job.done += len(event[2])
                elif kind == "end":
                    if cleaner is not None:
                        counts = report.setdefault(table.key, RestoreCounts())
                        counts.dropped += cleaner.dropped + cleaner.foreign + cleaner.unmarked + cleaner.over_limit
                        cleaner.finish()
                        remaining.discard(table.key)
                    cleaner = table = None
                    if not remaining and settings_seen:  # the rest of the file is rows nobody asked for
                        break
        return settings_raw

    async def _insert_rows(self, database: Database, table: Table, cleaner: "RowCleaner", rows: list[list], counts: RestoreCounts, renumbered: dict[str, dict]) -> None:
        if not rows:
            return
        columns = cleaner.columns
        names = ", ".join(quote(c) for c in columns)
        marks = ", ".join("?" for _ in columns)
        target = quote(table.name)
        fresh = rows
        moved: list[list] = []
        if table.name == "timers":
            fresh = await self._timers_to_add(database, columns, rows, counts)
        if table.rowid_key and table.rowid_key in columns:
            at = columns.index(table.rowid_key)
            existing: dict = {}
            ids = [row[at] for row in fresh if row[at] is not None]
            for part in _chunks_of(ids, 400):
                placeholders = ", ".join("?" for _ in part)
                for found in await database.all(f"SELECT {names} FROM {target} WHERE {quote(table.rowid_key)} IN ({placeholders})", part):
                    existing[found[table.rowid_key]] = [found[c] for c in columns]
            plain = []
            for row in fresh:
                holder = existing.get(row[at])
                if holder is None:
                    plain.append(row)
                elif holder == row or (cleaner.guild_at is not None and holder[cleaner.guild_at] == cleaner.guild_id):
                    # This server's own row, and a merge only adds what's missing. A row edited since the file was made keeps its newer state.
                    counts.already_here += 1
                    cleaner.remember(row)
                else:  # another server's row holds this number
                    moved.append(row)
            fresh = plain
        sql = f"INSERT INTO {target} ({names}) VALUES ({marks}) ON CONFLICT DO NOTHING"
        if fresh:
            await database.run("SAVEPOINT backup_batch")
            try:
                cursor = await database.conn.executemany(sql, fresh)
                inserted = cursor.rowcount
                await cursor.close()
                counts.added += inserted
                counts.already_here += len(fresh) - inserted
                for row in fresh:
                    cleaner.remember(row)
            except cipher.IntegrityError:  # a row that breaks a NOT NULL or CHECK rule, so find which
                await database.run("ROLLBACK TO backup_batch")
                for row in fresh:
                    try:
                        cursor = await database.conn.execute(sql, row)
                        counts.added += max(cursor.rowcount, 0)
                        await cursor.close()
                        cleaner.remember(row)
                    except cipher.IntegrityError:
                        counts.dropped += 1
            await database.run("RELEASE backup_batch")
        for row in moved:
            copy = list(row)
            old_id = copy[at]
            copy[at] = None
            cursor = await database.conn.execute(f"INSERT INTO {target} ({names}) VALUES ({marks})", copy)
            new_id = cursor.lastrowid
            await cursor.close()
            copy[at] = new_id
            renumbered.setdefault(table.key, {})[old_id] = new_id
            counts.added += 1
            counts.renumbered += 1
            cleaner.remember(copy)

    def _past_role_change(self, kind: Any, due_at: Any, now: float) -> bool:
        """A timer a module lists in role_timers that's past due ran back then or is out of date, and running it now could undo what staff have done since, such as a tempban made permanent."""
        return isinstance(due_at, (int, float)) and due_at <= now and any(kind in module.role_timers for module in self.bot.modules)

    async def _timers_to_add(self, database: Database, columns: list[str], rows: list[list], counts: RestoreCounts) -> list[list]:
        """A timer with a key is one job however many rows mention it, and the one scheduled now is the one to keep."""
        if "kind" not in columns or "key" not in columns:
            return rows
        at_kind, at_key = columns.index("kind"), columns.index("key")
        at_due = columns.index("due_at") if "due_at" in columns else None
        now = time.time()
        out = []
        for row in rows:
            if at_due is not None and self._past_role_change(row[at_kind], row[at_due], now):
                counts.dropped += 1
            elif row[at_key] is not None and await database.scalar("SELECT 1 FROM timers WHERE kind = ? AND key = ?", (row[at_kind], row[at_key])):
                counts.already_here += 1
            else:
                out.append(row)
        return out

    # ---- snapshots ----

    async def _write_snapshot(self, guild_id: int, *, kind: str, actor_name: str | None, include_messages: bool) -> dict:
        self._require_room()
        name = f"{uuid.uuid4().hex}.db"
        path = self._snapshot_path(guild_id, name)
        counts: dict[str, int] = {}
        blocks = self.export_stream(guild_id, include_messages=include_messages, counts=counts)
        async with contextlib.aclosing(blocks):
            async with contextlib.aclosing(self._gzip(blocks)) as packed:
                await self._store_stream(path, packed)
        size = path.stat().st_size
        snapshot_id = await self.db.insert(
            "INSERT INTO backup_snapshots (guild_id, kind, file, created_at, created_by, size, row_count, includes_messages) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, kind, name, time.time(), actor_name, size, sum(counts.values()), int(include_messages)),
        )
        await self._prune(guild_id)
        return await self.db.one("SELECT * FROM backup_snapshots WHERE id = ?", (snapshot_id,))

    async def _prune(self, guild_id: int) -> None:
        keep = {KIND_SCHEDULED: self.cfg(guild_id)["keep"], KIND_BEFORE_IMPORT: BEFORE_IMPORT_KEEP}
        staged = self._staged.get(guild_id)
        for kind, count in keep.items():
            old = await self.db.all(
                "SELECT id, file FROM backup_snapshots WHERE guild_id = ? AND kind = ? ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
                (guild_id, kind, count),
            )
            for row in old:
                # Someone may be looking at this one's preview, or be about to restore it.
                if staged is None or staged.path != self._snapshot_path(guild_id, row["file"]):
                    await self._delete_snapshot(guild_id, row)
        held = await self.db.scalar("SELECT COALESCE(SUM(size), 0) FROM backup_snapshots WHERE guild_id = ?", (guild_id,))
        if held > MAX_SNAPSHOT_BYTES:
            # The newest stays, however large, so a server always has a way back.
            for row in await self.db.all("SELECT id, file, size FROM backup_snapshots WHERE guild_id = ? AND kind != ? ORDER BY created_at, id LIMIT -1 OFFSET 0", (guild_id, KIND_MANUAL)):
                newest = await self.db.scalar("SELECT MAX(id) FROM backup_snapshots WHERE guild_id = ?", (guild_id,))
                if held <= MAX_SNAPSHOT_BYTES or row["id"] == newest:
                    break
                if staged is None or staged.path != self._snapshot_path(guild_id, row["file"]):
                    await self._delete_snapshot(guild_id, row)
                    held -= row["size"]

    async def _delete_snapshot(self, guild_id: int, row: dict) -> None:
        await self.db.run("DELETE FROM backup_snapshots WHERE id = ? AND guild_id = ?", (row["id"], guild_id))
        self._remove_file(self._snapshot_path(guild_id, row["file"]))

    async def _sweep_due(self, payload: dict, row: dict) -> None:
        """Start the snapshots in the background, because the timer loop runs one handler at a time and a large server takes a while."""
        await self.bot.timers.schedule(SWEEP_KIND, time.time() + SWEEP_SECONDS, {}, key="all")
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.ensure_future(self.take_due_snapshots())

    async def take_due_snapshots(self) -> int:
        """One snapshot for each server whose last scheduled one is old enough. Also clears uploads nobody finished with."""
        now = time.time()
        for guild_id, staged in list(self._staged.items()):
            if staged.owned and now - staged.created_at > UPLOAD_SECONDS and not self._is_busy(guild_id):
                self._drop_staged(guild_id)
        for guild_id, (_, _, made) in list(self._exports.items()):
            if now - made > UPLOAD_SECONDS:
                self._drop_export(guild_id)
        taken = 0
        for guild_id in self.bot.ops.guild_ids():
            cfg = self.cfg(guild_id)
            if not cfg["enabled"] or self._is_busy(guild_id):
                continue
            newest = await self.db.scalar("SELECT MAX(created_at) FROM backup_snapshots WHERE guild_id = ? AND kind = ?", (guild_id, KIND_SCHEDULED))
            # A little slack, so a snapshot that ran a few minutes long doesn't push every later one back by a whole sweep.
            if newest is not None and now - newest < cfg["interval_hours"] * 3600 - SWEEP_SECONDS / 2:
                continue

            async def work(job: Job, guild_id: int = guild_id, include: bool = cfg["include_messages"]) -> dict:
                job.stage = "Taking a scheduled snapshot"
                row = await self._write_snapshot(guild_id, kind=KIND_SCHEDULED, actor_name=None, include_messages=include)
                await self.bot.activity_log.record(guild_id, None, "backup.snapshot", detail=f"scheduled, {row['row_count']} rows")
                return {"snapshot": row["id"]}

            try:
                job = self._start_job(guild_id, "snapshot", work)
            except ApiError:  # someone started a job while this was looking
                continue
            await asyncio.wait({job.task})
            if job.error is None:
                taken += 1
        return taken

    # ---- jobs ----

    def _is_busy(self, guild_id: int) -> bool:
        job = self._jobs.get(guild_id)
        return job is not None and job.running

    def _start_job(self, guild_id: int, kind: str, work: Callable[[Job], Awaitable[Any]]) -> Job:
        if self._is_busy(guild_id):
            raise ApiError(409, "A backup job is already running for this server. Wait for it to finish.")
        job = Job(kind)
        self._jobs[guild_id] = job
        job.task = asyncio.ensure_future(self._run_job(job, work))
        return job

    async def _run_job(self, job: Job, work: Callable[[Job], Awaitable[Any]]) -> None:
        try:
            job.stage = "Waiting for another server's backup to finish"
            async with self._job_gate:
                job.stage = ""
                job.result = await work(job)
        except BackupError as error:
            job.error = str(error)
        except ApiError as error:
            job.error = error.message
        except asyncio.CancelledError:
            job.error = "Stopped." if job.stopped else "The bot restarted before this finished."
            raise
        except Exception:
            log.exception("Backup job %s failed", job.kind)
            job.error = "Something went wrong on the server. It's been logged."
        finally:
            job.running = False
            job.stage = ""

    def _drop_export(self, guild_id: int) -> None:
        held = self._exports.pop(guild_id, None)
        if held is not None:
            self._remove_file(held[1])

    def _start_export(self, guild_id: int, *, include_messages: bool) -> Job:
        """The file goes into an encrypted holding place first, and the download reads from there. A person's connection then has no say in how long a read of the database stays open, since an open read keeps the database's log from shrinking for every server."""

        async def work(job: Job) -> dict:
            job.stage = "Writing the file"
            name = f"export-{uuid.uuid4().hex}.db"
            path = self._snapshot_path(guild_id, name)
            blocks = self.export_stream(guild_id, include_messages=include_messages)
            async with contextlib.aclosing(blocks):
                async with contextlib.aclosing(self._gzip(blocks)) as packed:
                    await self._store_stream(path, packed)
            self._drop_export(guild_id)
            token = secrets.token_hex(16)
            self._exports[guild_id] = (token, path, time.time())
            return {"token": token}

        return self._start_job(guild_id, "export", work)

    def _drop_staged(self, guild_id: int) -> None:
        staged = self._staged.pop(guild_id, None)
        if staged is not None and staged.owned:
            self._remove_file(staged.path)

    def _stage(self, guild_id: int, staged: Staged) -> Job:
        """Put a file up for preview. A file that fails its preview goes away again."""
        if self._is_busy(guild_id):
            # A running job may be reading the file staged now, so that one stays and this one goes.
            if staged.owned:
                self._remove_file(staged.path)
            raise ApiError(409, "A backup job is already running for this server. Wait for it to finish.")
        self._drop_staged(guild_id)
        self._staged[guild_id] = staged

        async def work(job: Job) -> dict:
            try:
                return await self._build_preview(guild_id, staged, job)
            except Exception:
                if self._staged.get(guild_id) is staged:
                    self._drop_staged(guild_id)
                raise

        return self._start_job(guild_id, "preview", work)

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/backup", self.api_overview)
        router.get("/backup/export", self.api_export)
        router.post("/backup/exports", self.api_export_start)
        router.get("/backup/exports/{token}", self.api_export_file)
        router.get("/backup/job", self.api_job)
        router.post("/backup/upload", self.api_upload)
        router.post("/backup/restore", self.api_restore)
        router.delete("/backup/staged", self.api_cancel)
        router.post("/backup/snapshots", self.api_snapshot_now)
        router.get(r"/backup/snapshots/{snapshot_id:\d+}/download", self.api_snapshot_download)
        router.post(r"/backup/snapshots/{snapshot_id:\d+}/preview", self.api_snapshot_preview)
        router.delete(r"/backup/snapshots/{snapshot_id:\d+}", self.api_snapshot_delete)

    @staticmethod
    def _snapshot_row(row: dict) -> dict:
        return {
            "id": row["id"], "kind": row["kind"], "created_at": row["created_at"], "created_by": row["created_by"], "size": row["size"],
            "row_count": row["row_count"], "includes_messages": bool(row["includes_messages"]),
        }

    def _is_admin(self, req: Any) -> bool:
        guild = self.bot.get_guild(req.guild_id)
        if guild is None:  # the demo has no server behind it
            return True
        member = guild.get_member(req.user.id)
        return member is not None and (guild.owner_id == member.id or member.guild_permissions.administrator)

    def _require_admin(self, req: Any) -> None:
        """The file holds every member's stored messages, deleted ones too, and a restore replaces the whole setup. Manage Server alone doesn't reach that."""
        if not self._is_admin(req):
            raise ApiError(403, "Only an administrator can export, restore or remove this server's backups.")

    async def api_overview(self, req: Any) -> dict:
        snapshots = await self.db.all("SELECT * FROM backup_snapshots WHERE guild_id = ? ORDER BY created_at DESC, id DESC", (req.guild_id,))
        databases = {}
        left_out: list[str] = []
        for label, database in (("main", self.db), ("messages", self.message_db)):
            async with database.reader() as view:
                catalog = await self.catalogs((label, view))
                rows = 0
                for table in catalog.tables.values():
                    rows += await view.scalar(f"SELECT COUNT(*) FROM {quote(table.name)} WHERE {table.where}", (req.guild_id,))
            databases[label] = {"tables": len(catalog.tables), "rows": rows}
            left_out += catalog.left_out
        job = self._jobs.get(req.guild_id)
        return {
            "admin": self._is_admin(req),
            "snapshots": [self._snapshot_row(row) for row in snapshots],
            "disk_bytes": sum(row["size"] for row in snapshots),
            "databases": databases,
            "left_out": left_out,
            "job": job.state() if job else None,
        }

    async def api_job(self, req: Any) -> dict:
        job = self._jobs.get(req.guild_id)
        return {"job": job.state() if job else None}

    async def _download(self, req: Any, blocks: AsyncIterator[bytes], filename: str, content_type: str) -> web.StreamResponse:
        if self._downloads >= MAX_DOWNLOADS_AT_ONCE:
            await blocks.aclose()
            raise ApiError(409, "Too many backups are downloading right now. Try again in a few minutes.")
        self._downloads += 1
        try:
            headers = {**SECURITY_HEADERS, "Content-Type": content_type, "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"}
            response = web.StreamResponse(headers=headers)
            await response.prepare(req.request)
            async with contextlib.aclosing(blocks):
                try:
                    async for block in blocks:
                        await response.write(block)
                except ConnectionError:  # they closed the tab
                    return response
                except Exception:
                    # The headers have gone, so this can't become an error message. Cutting the connection makes the browser show a failed download.
                    log.exception("A backup download failed for %s", req.guild_id)
                    if req.request.transport is not None:
                        req.request.transport.abort()
                    return response
            await response.write_eof()
            return response
        finally:
            self._downloads -= 1

    async def api_export(self, req: Any) -> web.StreamResponse:
        self._require_admin(req)
        include_messages = req.query.get("messages", "1") != "0"
        await req.record("backup.export", detail=f"{'with' if include_messages else 'without'} messages and logs")
        job = self._start_export(req.guild_id, include_messages=include_messages)
        await asyncio.wait({job.task})
        if job.error:
            raise ApiError(409, job.error)
        return await self._export_download(req, req.query.get("gzip") == "1")

    async def api_export_start(self, req: Any) -> dict:
        """The page's way in: it follows the job, then downloads the file, so nothing sits waiting on one long request."""
        self._require_admin(req)
        form = await req.json()
        include_messages = bool(form.get("messages", True))
        job = self._start_export(req.guild_id, include_messages=include_messages)
        await req.record("backup.export", detail=f"{'with' if include_messages else 'without'} messages and logs")
        return {"job": job.state()}

    async def api_export_file(self, req: Any) -> web.StreamResponse:
        self._require_admin(req)
        held = self._exports.get(req.guild_id)
        if held is None or not secrets.compare_digest(req.param("token"), held[0]):
            raise ApiError(404, "That export has expired. Make it again.")
        return await self._export_download(req, req.query.get("gzip") == "1")

    async def _export_download(self, req: Any, packed: bool) -> web.StreamResponse:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        name = f"{brand.SLUG}-{req.guild_id}-{stamp}.json"
        stored = self._read_stored(self._exports[req.guild_id][1])
        if packed:
            return await self._download(req, stored, name + ".gz", "application/gzip")
        return await self._download(req, self._unpacked(stored), name, "application/json; charset=utf-8")

    @staticmethod
    async def _unpacked(stored: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async with contextlib.aclosing(stored):
            async with contextlib.aclosing(inflate(stored)) as plain:
                async for block in plain:
                    yield block

    async def api_upload(self, req: Any) -> dict:
        self._require_admin(req)
        if self._is_busy(req.guild_id):
            raise ApiError(409, "A backup job is already running for this server. Wait for it to finish.")
        length = req.request.content_length
        if length is not None and length > MAX_UPLOAD_BYTES:
            raise ApiError(413, "That file is too large to restore.")
        if req.guild_id in self._uploading:
            raise ApiError(409, "A file is already on its way up for this server. Wait for it to finish.")
        if len(self._uploading) >= MAX_UPLOADS_AT_ONCE:
            raise ApiError(409, "The bot is taking in files for other servers right now. Try again in a few minutes.")
        self._require_room()
        path = self._snapshot_path(req.guild_id, f"upload-{uuid.uuid4().hex}.db")
        self._uploading.add(req.guild_id)
        try:
            size = await self._store_stream(path, req.request.content.iter_chunked(CHUNK_BYTES), limit=MAX_UPLOAD_BYTES)
        finally:
            self._uploading.discard(req.guild_id)
        if size == 0:
            self._remove_file(path)
            raise ApiError(400, "That file is empty.")
        job = self._stage(req.guild_id, Staged(secrets.token_hex(16), path, True, "An uploaded file"))
        return {"job": job.state()}

    async def _own_snapshot(self, req: Any) -> dict:
        row = await self.db.one("SELECT * FROM backup_snapshots WHERE id = ? AND guild_id = ?", (req.int_param("snapshot_id"), req.guild_id))
        if row is None:
            raise ApiError(404, "There's no such snapshot.")
        return row

    async def api_snapshot_preview(self, req: Any) -> dict:
        self._require_admin(req)
        row = await self._own_snapshot(req)
        label = f"Snapshot from {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(row['created_at']))}"
        job = self._stage(req.guild_id, Staged(secrets.token_hex(16), self._snapshot_path(req.guild_id, row["file"]), False, label))
        return {"job": job.state()}

    async def api_snapshot_download(self, req: Any) -> web.StreamResponse:
        self._require_admin(req)
        row = await self._own_snapshot(req)
        if not self._snapshot_path(req.guild_id, row["file"]).exists():
            raise ApiError(404, "That snapshot's file is gone from the server.")
        await req.record("backup.download", detail=f"snapshot {row['id']}")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(row["created_at"]))
        return await self._download(req, self._read_stored(self._snapshot_path(req.guild_id, row["file"])), f"{brand.SLUG}-{req.guild_id}-{stamp}.json.gz", "application/gzip")

    async def api_snapshot_delete(self, req: Any) -> dict:
        self._require_admin(req)
        row = await self._own_snapshot(req)
        if self._is_busy(req.guild_id):
            raise ApiError(409, "A backup job is running for this server. Wait for it to finish.")
        staged = self._staged.get(req.guild_id)
        if staged is not None and staged.path == self._snapshot_path(req.guild_id, row["file"]):
            self._drop_staged(req.guild_id)
        await self._delete_snapshot(req.guild_id, row)
        await req.record("backup.snapshot.delete", detail=f"snapshot {row['id']}")
        return {"ok": True}

    async def api_snapshot_now(self, req: Any) -> dict:
        self._require_admin(req)
        manual = await self.db.scalar("SELECT COUNT(*) FROM backup_snapshots WHERE guild_id = ? AND kind = ?", (req.guild_id, KIND_MANUAL))
        if manual >= MANUAL_KEEP:
            raise ApiError(400, f"This server holds {MANUAL_KEEP} snapshots taken by hand. Delete one first.")
        if await self.db.scalar("SELECT COALESCE(SUM(size), 0) FROM backup_snapshots WHERE guild_id = ? AND kind = ?", (req.guild_id, KIND_MANUAL)) > MAX_SNAPSHOT_BYTES:
            raise ApiError(400, "This server's snapshots taken by hand have reached the limit on disk. Delete one first.")
        guild_id, actor = req.guild_id, req.actor
        include = self.cfg(guild_id)["include_messages"]

        async def work(job: Job) -> dict:
            job.stage = "Taking a snapshot"
            row = await self._write_snapshot(guild_id, kind=KIND_MANUAL, actor_name=actor.name, include_messages=include)
            await self.bot.activity_log.record(guild_id, actor, "backup.snapshot", detail=f"by hand, {row['row_count']} rows")
            return {"snapshot": row["id"]}

        return {"job": self._start_job(guild_id, "snapshot", work).state()}

    async def api_cancel(self, req: Any) -> dict:
        self._require_admin(req)
        if self._is_busy(req.guild_id):
            job = self._jobs[req.guild_id]
            if job.kind != "preview":  # a restore or a snapshot has to finish, or it would leave things half done
                raise ApiError(409, "A backup job is running for this server. Wait for it to finish.")
            job.stopped = True
            job.task.cancel()
            await asyncio.wait({job.task})
        self._drop_staged(req.guild_id)
        return {"ok": True}

    async def api_restore(self, req: Any) -> dict:
        self._require_admin(req)
        form = await req.json()
        token, items = form.get("token") if isinstance(form, dict) else None, form.get("items") if isinstance(form, dict) else None
        staged = self._staged.get(req.guild_id)
        if staged is None or not isinstance(token, str) or not secrets.compare_digest(token, staged.token):
            raise ApiError(409, "That preview has expired. Open the file again.")
        if not isinstance(items, list) or not items or len(items) > 5000 or not all(isinstance(item, str) for item in items):
            raise ApiError(400, "Pick at least one thing to restore.")
        guild_id, actor, chosen = req.guild_id, req.actor, set(items)

        async def work(job: Job) -> dict:
            result = await self._restore(guild_id, staged, chosen, actor, job)
            if self._staged.get(guild_id) is staged:
                self._drop_staged(guild_id)
            return result

        return {"job": self._start_job(guild_id, "restore", work).state()}

    # ---- for tests and for code that wants to wait ----

    async def wait_for_job(self, guild_id: int) -> Job:
        job = self._jobs[guild_id]
        if job.task is not None:
            await asyncio.wait({job.task})
        return job


class RowCleaner:
    """Turns one table's rows from a file into values safe to store here. The same checks serve the preview and the restore, so the preview tells the truth."""

    def __init__(self, cog: Backup, guild_id: int, table: Table, file_columns: list[str], parents: dict[tuple[str, str], set], catalog: Catalog, renumbered: dict[str, dict] | None = None, budget: list[int] | None = None) -> None:
        self.cog = cog
        self.guild_id = guild_id
        self.table = table
        self.expected = len(file_columns)
        self.positions = [(index, name) for index, name in enumerate(file_columns) if name in table.affinity]
        self.columns = [name for _, name in self.positions]
        self.extra_columns = [name for name in file_columns if name not in table.affinity and name != MARK_COLUMN]
        self.mark_at = file_columns.index(MARK_COLUMN) if MARK_COLUMN in file_columns else None
        self.file_columns = file_columns
        self.unmarked = 0
        self.untrusted = 0
        # Every row here would stay out, and replacing a table with nothing would lose what's here now. A history only adds, so it loses nothing.
        self.unprovable = table.key in MARKED_TABLES and table.key not in FLAG_WITHOUT_MARK and self.mark_at is None and not table.merges
        self.adds_guild = "guild_id" in table.affinity and "guild_id" not in self.columns
        if self.adds_guild:
            self.columns.append("guild_id")
        self.guild_at = self.columns.index("guild_id") if "guild_id" in self.columns else None
        self.number_at = self.columns.index(table.rowid_key) if table.autoincrement and table.rowid_key in self.columns else None
        self.missing_required = [name for name in table.required if name not in self.columns]
        self.parents = parents
        self.renumbered = renumbered if renumbered is not None else {}
        self.checks = [(self.columns.index(link.column), link) for link in table.links if link.column in self.columns]
        self.referenced = [(column, self.columns.index(column)) for column in catalog.referenced_columns(table) if column in self.columns]
        self.scheduled_now: set[tuple] = set()
        self.dropped = 0
        self.foreign = 0
        self.limit = next((module.row_limits[table.name] for module in cog.bot.modules if table.name in module.row_limits), None)
        self.kept = 0
        self.over_limit = 0
        self.budget = budget

    def clean(self, raw: Any) -> list | None:
        if self.missing_required or not isinstance(raw, list) or len(raw) != self.expected:
            self.dropped += 1
            return None
        if self.table.key in MARKED_TABLES and not self._has_good_mark(raw):
            flag = FLAG_WITHOUT_MARK.get(self.table.key)
            if flag is None:
                self.unmarked += 1
                return None
            if flag in self.file_columns:
                raw = [*raw]
                self.untrusted += 1 if raw[self.file_columns.index(flag)] else 0
                raw[self.file_columns.index(flag)] = 0
        try:
            values = []
            for index, name in self.positions:
                cell = raw[index]
                if self.cog._is_sealed(cell):
                    # Sealed under this table and column, so it can't be moved somewhere a person could read it.
                    cell = self.cog._unseal(self.guild_id, f"{self.table.key}.{name}", cell)
                elif cell and _SECRET_COLUMN.search(name):
                    cell = None  # a secret written in plain text didn't come from this bot
                values.append(decode_cell(cell, self.table.affinity[name]))
        except BadCell:
            self.dropped += 1
            return None
        if self.number_at is not None and isinstance(values[self.number_at], int) and abs(values[self.number_at]) > JS_SAFE_INT:
            # Every server shares this table's counter, and one row numbered near the top would leave none of them able to add a row.
            self.dropped += 1
            return None
        if self.adds_guild:
            values.append(self.guild_id)
        elif self.guild_at is not None:
            values[self.guild_at] = self.guild_id
        if self.table.key == "main.activity":
            values = self.cog._activity_in(self.guild_id, self.columns, values)
        # The message store holds what members wrote, and a member can paste JSON. Only the main database's text is the bot's own.
        if self.cog._row_reaches_elsewhere(self.guild_id, self.columns, values, read_json=self.table.database == "main"):
            self.foreign += 1
            return None
        for at, link in self.checks:
            parent_key = f"{self.table.database}.{link.parent}"
            value = self.renumbered.get(parent_key, {}).get(values[at], values[at])
            values[at] = value
            if value is None:
                continue
            known = self.parents.get((parent_key, link.parent_column))
            # A file from an older version can hold this table ahead of its parent, from before the two were linked. Backup._drop_strays checks those rows once both tables are in, which it can do only for rows that carry their own server.
            if known is None and not link.declared and self.guild_at is not None:
                continue
            if value not in (known or ()):
                self.foreign += 1
                return None
        if self.limit is not None and self.kept >= self.limit:
            self.over_limit += 1
            return None
        if self.budget is not None and self.table.database == "main":
            if self.budget[0] <= 0:
                self.over_limit += 1
                return None
            self.budget[0] -= 1
        self.kept += 1
        return values

    def _has_good_mark(self, raw: list) -> bool:
        names = MARKED_TABLES[self.table.key]
        if self.mark_at is None or not all(name in self.file_columns for name in names):
            return False
        mark = raw[self.mark_at]
        expected = self.cog._row_mark(self.guild_id, self.table.key, [raw[self.file_columns.index(name)] for name in names])
        return isinstance(mark, str) and hmac.compare_digest(mark, expected)

    def finish(self) -> None:
        """This table has been read, so a child that comes later is checked against what it held, even if that was nothing."""
        for column, _ in self.referenced:
            self.parents.setdefault((self.table.key, column), set())

    def remember(self, values: list) -> None:
        """This row is staying, so rows that point at it may stay too."""
        for column, at in self.referenced:
            known = self.parents.setdefault((self.table.key, column), set())
            if len(known) >= MAX_LINKED_IDS:
                raise BackupError("This file holds more linked rows than can be checked.")
            known.add(values[at])
