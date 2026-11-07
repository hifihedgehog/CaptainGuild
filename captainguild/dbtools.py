"""Copy or read the encrypted databases from the command line.

    python -m captainguild.dbtools copy main data/main.db backups/main-copy.db
    python -m captainguild.dbtools decrypt main backups/main-copy.db readable.db
    python -m captainguild.dbtools restore backups/main-copy.db data/main.db

The word after copy or decrypt is which database: main or messages. These read MASTER_KEY from the environment or .env. A copy can
run while the bot does and opens with the same master key. A decrypted file is plain SQLite for recovery, and only its owner can
read it. Restore puts an older copy of the main database in place while the bot is stopped, and marks it, so the next start treats
what it holds as restored rather than as time gone by: role changes already past due don't run, and interrupted jobs don't pick up.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlcipher3 import dbapi2 as cipher

from .crypto import Keyring, decode_master_key
from .db import _open_encrypted

FILE_RESTORED_KEY = "file_restored_at"


class ToolError(RuntimeError):
    """The copy failed its checks, so nothing was put in place."""


def _problems(connection) -> list[str]:
    bad = [row[0] for row in connection.execute("PRAGMA cipher_integrity_check")]
    if bad:
        return bad
    return [row[0] for row in connection.execute("PRAGMA integrity_check") if row[0] != "ok"]


def _mark_restored(connection: Any) -> bool:
    """Leaves the note for the bot's next start. False when the file has nowhere to hold it, so it isn't this bot's."""
    try:
        connection.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (FILE_RESTORED_KEY, str(time.time())))
        connection.commit()
    except cipher.DatabaseError:
        return False
    return True


def copy_database(source: str | Path, target: str | Path, key: str, *, restoring: bool = False) -> None:
    """Use SQLite's online backup, check the copy, and put it in place with one rename. An older copy stays until the new one passes.

    With `restoring`, the copy is marked for the bot's next start, and what's left of the file it replaces goes with it. A write-ahead log from the old file would be read as part of the new one.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.new")
    temporary.unlink(missing_ok=True)
    reader = _open_encrypted(f"{Path(source).resolve().as_uri()}?mode=ro", key, uri=True)
    try:
        writer = _open_encrypted(str(temporary), key)
        try:
            reader.backup(writer)
            problems = [] if not restoring or _mark_restored(writer) else ["That file isn't a copy of this bot's main database."]
            problems = problems or _problems(writer)
        finally:
            writer.close()
    finally:
        reader.close()
    if problems:
        temporary.unlink(missing_ok=True)
        raise ToolError(f"The copy failed its checks: {problems[0]}")
    if restoring:
        for leftover in ("-wal", "-shm"):
            target.with_name(target.name + leftover).unlink(missing_ok=True)
    os.replace(temporary, target)


def decrypt(source: str | Path, target: str | Path, key: str) -> None:
    """Export to a new plain file. Refuses to overwrite anything, and the file is private before any row is in it."""
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise ToolError(f"{source} doesn't exist.")
    if target.exists():
        raise ToolError(f"{target} already exists. Move it away first.")
    connection = _open_encrypted(f"{source.resolve().as_uri()}?mode=ro", key, uri=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        connection.execute("ATTACH DATABASE ? AS readable KEY ''", (str(target),))
        connection.execute("SELECT sqlcipher_export('readable')").fetchall()
        connection.commit()
        connection.execute("DETACH DATABASE readable")
    except Exception:
        connection.close()
        target.unlink(missing_ok=True)
        raise
    connection.close()


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    restoring = len(arguments) == 3 and arguments[0] == "restore"
    if not restoring and (len(arguments) != 4 or arguments[0] not in ("copy", "decrypt") or arguments[1] not in ("main", "messages")):
        print("Usage: python -m captainguild.dbtools copy|decrypt main|messages SOURCE TARGET", file=sys.stderr)
        print("       python -m captainguild.dbtools restore SOURCE TARGET", file=sys.stderr)
        return 2
    load_dotenv()
    master = os.environ.get("MASTER_KEY")
    if not master:
        print("MASTER_KEY is missing from the environment and .env.", file=sys.stderr)
        return 2
    key = Keyring(decode_master_key(master)).database_key("main" if restoring else arguments[1])
    try:
        if restoring:
            copy_database(arguments[1], arguments[2], key, restoring=True)
        else:
            (copy_database if arguments[0] == "copy" else decrypt)(arguments[2], arguments[3], key)
    except ToolError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
