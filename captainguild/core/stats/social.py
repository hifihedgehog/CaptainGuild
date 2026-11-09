"""Follower counts from YouTube, Instagram and TikTok, read from each site's public page for an account, with no key.

Each site embeds the account's numbers in its page for its own scripts:
- YouTube, in ytInitialData: the channel header's line that ends in "subscribers", such as "246K subscribers". YouTube rounds it to three
  significant digits, and so does the count here.
- Instagram, in the JSON its profile page carries: the profile's follower_count, which is exact. The page's description, such as "686M
  Followers", stands in when the JSON isn't there.
- TikTok, in __UNIVERSAL_DATA_FOR_REHYDRATION__: the user's statsV2 followerCount, which is exact, or stats followerCount, which is rounded.

A count read well is kept for an hour, and every counter naming that account shares it. One that fails is tried again after 15 minutes,
then after longer waits up to six hours, and meanwhile the last good count stays, with the reason it's old. Reads of one site never run at
once and wait SITE_GAP seconds after the one before, so many servers following the same sites ask little of them.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..outside import Outside, OutsideError
from .counters import short_count

SITES = ("youtube", "instagram", "tiktok")
FRESH_SECONDS = 3600.0
RETRY_SECONDS = (900.0, 1800.0, 3600.0, 7200.0, 21600.0)
SITE_GAP = 5.0
PAGE_BYTES = 8 * 1024 * 1024
TIMEOUT = 20.0
MAX_KEPT = 5000  # accounts remembered across every server
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
YOUTUBE = "https://www.youtube.com"
YOUTUBE_HEADERS = {"Cookie": "SOCS=CAI; CONSENT=YES+", "Accept-Language": "en-US,en;q=0.8"}  # the cookie skips the consent page sent to European visitors
INSTAGRAM_HEADERS = {"User-Agent": BROWSER, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.8", "Sec-Fetch-Dest": "document",
                     "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"}  # the profile's JSON comes only with a page load that looks like a browser's
TIKTOK_HEADERS = {"User-Agent": BROWSER, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.8"}
NOT_FOUND = "The site has no account with that name."
PRIVATE = "That account is private, so its count isn't shown."
CHANGED = "The page didn't hold a follower count. The site may have changed how it shows one, or asked the bot to sign in."


class SocialError(Exception):
    """The count couldn't be read. The text is for the person reading the page."""


@dataclass
class Count:
    value: int | None
    problem: str | None = None
    read_at: float | None = None


@dataclass
class _Held:
    value: int | None = None
    read_at: float | None = None
    problem: str | None = None
    failures: int = 0
    next_try: float = 0.0


# ---- reading the pages ----

def _json_after(page: str, start: re.Pattern) -> Any:
    found = start.search(page)
    if not found:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(page, found.end())
    except ValueError:
        return None
    return value


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def youtube_count(page: str) -> int | None:
    """The subscriber count a channel page shows. None when the page shows none."""
    data = _json_after(page, re.compile(r"var ytInitialData\s*=\s*"))
    if not isinstance(data, dict):
        return None
    header = data.get("header") or {}
    for node in _walk(header):
        for key in ("subscriberCountText",):
            if key in node:
                found = _youtube_text(node[key])
                if found is not None:
                    return found
        content = (node.get("text") or {}).get("content") if isinstance(node.get("text"), dict) else None
        if isinstance(content, str) and "subscriber" in content.lower():
            return _youtube_text(content)
    return None


def _youtube_text(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("simpleText") or "".join(run.get("text", "") for run in value.get("runs", []) if isinstance(run, dict))
    if not isinstance(value, str):
        return None
    if value.strip().lower().startswith("no "):
        return 0
    return short_count(value)


def instagram_count(page: str) -> int | None:
    """The follower count a profile page carries: exact from its JSON, or the rounded one in its description."""
    for script in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(script)
        except ValueError:
            continue
        for node in _walk(data):
            box = node.get("__bbox")
            result = box.get("result") if isinstance(box, dict) else None
            found = result.get("data") if isinstance(result, dict) else None
            if not isinstance(found, dict):
                continue
            user = found.get("xig_user_by_username")
            if isinstance(user, dict):
                count = user.get("follower_count")
                if count is None and isinstance(user.get("edge_followed_by"), dict):
                    count = user["edge_followed_by"].get("count")
                if isinstance(count, int) and not isinstance(count, bool):
                    return count
    for content in re.findall(r'<meta[^>]+(?:property|name)="(?:og:)?description"[^>]+content="([^"]*)"', page):
        found = re.match(r"\s*([\d.,]+\s*[KkMmBb]?)\s+Followers", html.unescape(content))
        if found:
            return short_count(found.group(1))
    return None


def instagram_missing(page: str) -> bool:
    return '"pageID":"httpErrorPage"' in page


def tiktok_count(page: str) -> tuple[int | None, int | None]:
    """(the follower count, TikTok's status code for the profile). 10221 is no such account and 10222 a private one."""
    data = _json_after(page, re.compile(r'<script[^>]+\bid="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>'))
    detail = ((data or {}).get("__DEFAULT_SCOPE__") or {}).get("webapp.user-detail") if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return None, None
    code = detail.get("statusCode")
    info = detail.get("userInfo") or {}
    for key in ("statsV2", "stats"):
        stats = info.get(key) or {}
        count = stats.get("followerCount")
        if isinstance(count, str) and count.isdigit():
            return int(count), code
        if isinstance(count, int) and not isinstance(count, bool):
            return count, code
    return None, code


# ---- the reader ----

class Followers:
    """Reads and keeps follower counts. Tests give it a stand-in for Outside."""

    def __init__(self, outside: Any = None, clock: Callable[[], float] = time.time, gap: float = SITE_GAP) -> None:
        self.outside = outside or Outside()
        self.clock = clock
        self.gap = gap
        self._held: dict[tuple[str, str], _Held] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._site_locks = {site: asyncio.Lock() for site in SITES}
        self._last_request: dict[str, float] = {}

    async def count(self, site: str, account: str, now: float | None = None) -> Count:
        """The account's count, read again when the one held is over an hour old, and kept with the reason when a read fails."""
        key = (site, account.lower())
        now = self.clock() if now is None else now
        held = self._held.get(key)
        if held is not None and held.value is not None and held.read_at is not None and now - held.read_at < FRESH_SECONDS:
            return Count(held.value, None, held.read_at)
        if held is not None and now < held.next_try:
            return self._answer(held)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            held = self._held.get(key) or _Held()
            if held.read_at is not None and held.value is not None and now - held.read_at < FRESH_SECONDS:
                return Count(held.value, None, held.read_at)
            if now < held.next_try:
                return self._answer(held)
            try:
                value = await self.read(site, account)
            except SocialError as error:
                held.failures += 1
                held.problem = str(error)
                held.next_try = now + RETRY_SECONDS[min(held.failures, len(RETRY_SECONDS)) - 1]
            else:
                held.value, held.read_at, held.problem, held.failures, held.next_try = value, now, None, 0, 0.0
            self._keep(key, held)
            return self._answer(held)

    def _answer(self, held: _Held) -> Count:
        if held.problem and held.value is not None:
            return Count(held.value, f"Showing the count from earlier. The last read failed: {held.problem}", held.read_at)
        return Count(held.value, held.problem, held.read_at)

    def _keep(self, key: tuple[str, str], held: _Held) -> None:
        self._held[key] = held
        if len(self._held) > MAX_KEPT:
            oldest = min(self._held, key=lambda name: self._held[name].read_at or 0.0)
            self._held.pop(oldest, None)
            self._locks.pop(oldest, None)

    async def read(self, site: str, account: str) -> int:
        """One read of the site's page, after the polite wait. Raises SocialError with the sentence to show."""
        if site not in SITES:
            raise SocialError("That site isn't one the bot reads.")
        async with self._site_locks[site]:
            wait = self._last_request.get(site, 0.0) + self.gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await getattr(self, f"_{site}")(account)
            finally:
                self._last_request[site] = time.monotonic()

    async def _get(self, url: str, headers: dict) -> Any:
        try:
            return await self.outside.get(url, headers=headers, max_bytes=PAGE_BYTES, timeout=TIMEOUT)
        except OutsideError as error:
            raise SocialError(str(error)) from error

    async def _youtube(self, account: str) -> int:
        path = f"/channel/{account}" if account.startswith("UC") else f"/{account if account.startswith('@') else '@' + account}"
        reply = await self._get(YOUTUBE + path, YOUTUBE_HEADERS)
        if reply.status == 404:
            raise SocialError(NOT_FOUND)
        if reply.status != 200:
            raise SocialError(f"YouTube answered with error {reply.status}.")
        count = youtube_count(reply.text())
        if count is None:
            raise SocialError(CHANGED)
        return count

    async def _instagram(self, account: str) -> int:
        reply = await self._get(f"https://www.instagram.com/{account}/", INSTAGRAM_HEADERS)
        if reply.status == 404:
            raise SocialError(NOT_FOUND)
        if reply.status != 200:
            raise SocialError(f"Instagram answered with error {reply.status}.")
        page = reply.text()
        count = instagram_count(page)
        if count is None:
            raise SocialError(NOT_FOUND if instagram_missing(page) else CHANGED)
        return count

    async def _tiktok(self, account: str) -> int:
        reply = await self._get(f"https://www.tiktok.com/@{account}", TIKTOK_HEADERS)
        if reply.status == 404:
            raise SocialError(NOT_FOUND)
        if reply.status != 200:
            raise SocialError(f"TikTok answered with error {reply.status}.")
        count, code = tiktok_count(reply.text())
        if code == 10221:
            raise SocialError(NOT_FOUND)
        if code == 10222:
            raise SocialError(PRIVATE)
        if count is None:
            raise SocialError(CHANGED)
        return count

    def forget(self, site: str, account: str) -> None:
        """Drop what's held, so the next count reads the page again."""
        self._held.pop((site, account.lower()), None)

    async def close(self) -> None:
        await self.outside.close()
