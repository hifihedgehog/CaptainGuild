"""The test package. It also watches database connections: aiosqlite gives each one a worker thread that isn't a daemon, so a connection
left open keeps the test process from ever exiting. When a test module ends with one still open, this prints where each was opened and
ends the process with an error, instead of a run that hangs until something kills it."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import aiosqlite.core

_open = aiosqlite.core.Connection.__init__


def _remember_where(self, *args, **kwargs) -> None:
    _open(self, *args, **kwargs)
    self._thread.opened_at = "".join(traceback.format_stack(limit=30)[:-1])


def _report_open_connections() -> None:
    """Runs as the interpreter starts to wait for its threads, before it would wait on these forever."""
    stuck = [thread for thread in threading.enumerate() if thread.is_alive() and hasattr(thread, "opened_at")]
    # A connection closed just before the end hands back its result a moment before its thread ends, so each gets a moment to finish.
    # One left open waits on its queue forever and is still alive after it.
    deadline = time.monotonic() + 5
    for thread in stuck:
        thread.join(max(0.0, deadline - time.monotonic()))
    stuck = [thread for thread in stuck if thread.is_alive()]
    if not stuck:
        return
    sys.stdout.flush()  # os._exit below skips the usual flush, and the test results printed so far belong in the log
    sys.stderr.write(f"\n{len(stuck)} database connection(s) were never closed, so this process couldn't exit. Each was opened here:\n")
    for thread in stuck:
        sys.stderr.write(f"\n{thread.opened_at}")
    sys.stderr.flush()
    os._exit(3)


aiosqlite.core.Connection.__init__ = _remember_where
threading._register_atexit(_report_open_connections)
