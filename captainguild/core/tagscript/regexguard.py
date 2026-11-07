"""Patterns a server wrote, run where they can't stall the bot.

Python's re module holds the interpreter's lock for a whole match, so a pattern that backtracks for minutes would freeze every thread, and a timeout in a thread
would never get to fire. Patterns therefore run in a small child process that can simply be killed. One process serves every server, one request at a time, and
it comes back on the next request after a kill. Compiling happens there too, since a short pattern can take seconds to compile: a set of characters spanning all of Unicode, with case ignored, walks a million of them.

A pattern that runs out of time on some text skips that text for a while, so a member can't stall replies by sending it again, and the pattern goes on judging everything else. One crafted message must not switch a pattern off for a whole server.
Varied text is a different attack, so a server also has an allowance of time lost to timeouts each minute. Past a few seconds its patterns get a short clock, which is plenty for the text they're written for and cuts what each crafted message costs.
Past the whole allowance its patterns wait for the next minute, so one server can't keep the worker from the others.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time

log = logging.getLogger(__name__)

TIME_LIMIT = 1.0
START_LIMIT = 15.0
SKIP_SECONDS = 600
SHORT_LIMIT = 0.25
SLOW_WINDOW = 60.0
SHORT_AFTER = 3.0        # seconds lost to timeouts in a minute before a server's patterns get the short clock
STOP_AFTER = 15.0        # seconds lost to timeouts in a minute before a server's patterns wait for the next one
MAX_REMEMBERED = 5000
MAX_PATTERN = 1000
MAX_TEXT = 4000

# The alarm is for a bot that dies while a pattern runs: nothing is left to kill the worker then, so where the system offers an alarm it ends itself.
_WORKER = r"""
import json, re, signal, sys
alarm = getattr(signal, "alarm", lambda seconds: None)
cache = {}
print("ready", flush=True)
for line in sys.stdin:
    request = json.loads(line)
    alarm(30)
    try:
        pattern = cache.get(request["p"])
        if pattern is None:
            if len(cache) > 500:
                cache.clear()
            pattern = cache[request["p"]] = re.compile(request["p"])
        print(json.dumps({"m": pattern.search(request["t"]) is not None}), flush=True)
    except Exception as error:
        print(json.dumps({"e": str(error)[:200]}), flush=True)
    alarm(0)
"""


class RegexGuard:
    def __init__(self, time_limit: float = TIME_LIMIT) -> None:
        self.time_limit = time_limit
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._skip_until: dict[tuple[str, bytes], float] = {}         # (pattern, digest of the text) -> until when
        self._slow_until: dict[tuple[int | None, str], float] = {}    # (server, pattern) -> until when it counts as slow
        self._told: dict[tuple[int | None, str], float] = {}
        self._lost: dict[int | None, tuple[float, float]] = {}        # server -> (when its minute began, seconds lost to timeouts in it)
        self._failed_start = False
        self._closing = False
        self._saving: RegexGuard | None = None

    def took_too_long(self, pattern: str, server: int | None = None) -> bool:
        """Whether this pattern has run out of time on something lately, for the page and for anyone running it over many messages."""
        until = self._slow_until.get((server, pattern))
        return until is not None and time.monotonic() < until

    def first_slow_notice(self, pattern: str, server: int | None = None) -> bool:
        """True once per spell of slowness, so staff hear about a slow pattern without hearing about every message."""
        now = time.monotonic()
        key = (server, pattern)
        if not self.took_too_long(pattern, server) or now < self._told.get(key, 0.0):
            return False
        self._told[key] = now + SKIP_SECONDS
        return True

    def _lost_seconds(self, server: int | None, now: float) -> float:
        start, seconds = self._lost.get(server, (now, 0.0))
        return 0.0 if now - start >= SLOW_WINDOW else seconds

    def _lose_time(self, server: int | None, seconds: float) -> None:
        now = time.monotonic()
        lost = self._lost_seconds(server, now)
        self._lost[server] = (self._lost[server][0] if lost else now, lost + seconds)

    def is_stopped(self, server: int | None = None) -> bool:
        """Whether this server has lost its whole allowance of time this minute, so its patterns aren't being run."""
        return self._lost_seconds(server, time.monotonic()) >= STOP_AFTER

    def _forget_past(self) -> None:
        now = time.monotonic()
        for remembered in (self._skip_until, self._slow_until, self._told):
            if len(remembered) >= MAX_REMEMBERED:
                for key in [key for key, until in remembered.items() if now >= until]:
                    del remembered[key]
            while len(remembered) >= MAX_REMEMBERED:  # a member can send endless different text
                del remembered[min(remembered, key=remembered.__getitem__)]
        if len(self._lost) >= MAX_REMEMBERED:
            self._lost = {server: entry for server, entry in self._lost.items() if now - entry[0] < SLOW_WINDOW}

    def _note_slow(self, server: int | None, pattern: str, text_key: tuple[str, bytes], limit: float) -> None:
        self._forget_past()
        until = time.monotonic() + SKIP_SECONDS
        self._skip_until[text_key] = until
        self._slow_until[(server, pattern)] = until
        self._lose_time(server, limit)

    async def _ensure(self) -> asyncio.subprocess.Process | None:
        if self._process is not None and self._process.returncode is None:
            return self._process
        try:
            self._process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", "-c", _WORKER,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            # Starting Python can take a while on a busy machine, and that time mustn't count against the first pattern.
            await asyncio.wait_for(self._process.stdout.readline(), START_LIMIT)
        except asyncio.CancelledError:
            await self._kill()
            raise
        except (OSError, NotImplementedError, TimeoutError) as error:
            if not self._failed_start:
                log.error("Could not start the pattern worker, so regex triggers won't match: %s", error)
                self._failed_start = True
            await self._kill()
            return None
        self._failed_start = False
        return self._process

    async def _kill(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()

    async def _ask(self, pattern: str, text: str, limit: float | None = None) -> dict | None:
        """The worker's answer, or None when it can't be had. TimeoutError comes out for the caller to judge. A caller that gets canceled leaves no answer in the pipe for the next one to read as its own."""
        process = await self._ensure()
        if process is None:
            return None
        try:
            process.stdin.write((json.dumps({"p": pattern, "t": text[:MAX_TEXT]}) + "\n").encode())
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), self.time_limit if limit is None else limit)
        except (TimeoutError, asyncio.CancelledError):
            await self._kill()
            raise
        except (ConnectionError, OSError):
            await self._kill()
            return None
        if not line:
            await self._kill()
            return None
        return json.loads(line)

    async def search(self, pattern: str, text: str, server: int | None = None) -> bool:
        """Whether the pattern is found in the text. False when it's broken, or too slow on this text, or when `server` has used up its allowance of time."""
        text_key = (pattern, hashlib.blake2b(text[:MAX_TEXT].encode("utf-8", "surrogatepass"), digest_size=16).digest())
        until = self._skip_until.get(text_key)
        if until is not None:
            if time.monotonic() < until:
                return False
            del self._skip_until[text_key]
        async with self._lock:
            lost = self._lost_seconds(server, time.monotonic())
            if lost >= STOP_AFTER or self._closing:
                return False
            limit = min(self.time_limit, SHORT_LIMIT) if lost >= SHORT_AFTER else self.time_limit
            try:
                answer = await self._ask(pattern, text, limit)
            except TimeoutError:
                self._note_slow(server, pattern, text_key, limit)
                log.warning("A pattern ran out of time on one message, and skips that text for a while: %r", pattern[:100])
                return False
        return bool(answer and answer.get("m"))

    async def problem(self, pattern: str) -> str | None:
        """Why a pattern can't be saved, in a sentence for the person writing it. Saving has its own worker, so someone trying slow patterns can't hold up the patterns other servers are matching."""
        if len(pattern) > MAX_PATTERN:
            return f"A pattern can be at most {MAX_PATTERN} characters."
        if self._saving is None:
            self._saving = RegexGuard(self.time_limit)
        worker = self._saving
        async with worker._lock:
            try:
                answer = await worker._ask(pattern, "")
            except TimeoutError:
                return "That pattern takes too long to read."
        if answer is not None and "e" in answer:
            return f"That isn't a valid pattern: {answer['e']}"
        return None

    async def close(self) -> None:
        self._closing = True  # searches waiting for their turn give up, or a flood of them would take up a stop's time
        if self._saving is not None:
            await self._saving.close()
        async with self._lock:
            await self._kill()
