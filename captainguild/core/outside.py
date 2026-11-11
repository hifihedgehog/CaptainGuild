"""Calls to services outside Discord: Twitch, YouTube, the game deal sites, and image links members give the bot.

A module holds one of these as `self.outside`, so a test or the demo can swap in a fake and nothing else changes. Every call has a
time limit and a cap on how much it reads, and every failure arrives as OutsideError, so a polling loop has one thing to catch.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import ThreadedResolver
from yarl import URL

from .. import __version__, brand

# CheapShark refuses a generic User-Agent, aiohttp's default among them, and asks for one that names the application.
USER_AGENT = f"{brand.NAME.replace(' ', '')}/{__version__} ({brand.REPO_URL or 'self-hosted Discord bot'})"
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
MAX_REDIRECTS = 3
REDIRECTS = (301, 302, 303, 307, 308)


class OutsideError(Exception):
    """The service couldn't be reached, took too long or sent too much. The text is safe to log and to show."""


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError as error:
            raise OutsideError("The answer wasn't valid JSON.") from error

    def retry_after(self, default: float) -> float:
        """How long the service asked us to wait, in seconds."""
        try:
            return max(0.0, float(self.header("Retry-After") or default))
        except ValueError:
            return default


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def _is_public(address: str) -> bool:
    """Whether an address is on the public internet. An IPv6 address that carries an IPv4 one is judged by what it carries, because that's where a gateway sends it."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6:
        if ip in _NAT64 or ip in _IPV4_COMPATIBLE:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        else:
            ip = ip.ipv4_mapped or ip.sixtofour or ip
    return ip.is_global and not ip.is_multicast and not (ip.version == 6 and ip.is_site_local)


class _PublicResolver(ThreadedResolver):
    """Refuses a name that points inside a private network. Checking here also covers a name that changes between two lookups."""

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list:
        rows = await super().resolve(host, port, family)
        public = [row for row in rows if _is_public(row["host"])]
        if not public:
            raise OSError(f"{host} has no public address")
        return public


def check_public_link(url: str) -> None:
    """Refuse a link a member gave us unless it's plain http or https to a public address.

    aiohttp skips the resolver for an IP address, so the host is read here the way the client reads it. yarl folds full-width digits and dots to plain ones, which turns what looks like a name into an address.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
        host = URL(url).host
    except ValueError as error:
        raise OutsideError("That isn't a usable link.") from error
    if parts.scheme not in ("http", "https") or not host:
        raise OutsideError("Links must start with http:// or https://.")
    if parts.username or parts.password or port not in (None, 80, 443):
        raise OutsideError("That link can't be used.")
    try:
        public = _is_public(host.rstrip("."))
    except ValueError:
        return
    if not public:
        raise OutsideError("That link points to a private address.")


class Outside:
    offline = False  # the tests set this, so nothing they run can leave the machine by accident

    def __init__(self) -> None:
        self._plain: aiohttp.ClientSession | None = None
        self._guarded: aiohttp.ClientSession | None = None

    def _session(self, stranger: bool) -> aiohttp.ClientSession:
        if stranger:
            if self._guarded is None or self._guarded.closed:
                self._guarded = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=_PublicResolver()), cookie_jar=aiohttp.DummyCookieJar())
            return self._guarded
        if self._plain is None or self._plain.closed:
            self._plain = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        return self._plain

    async def get(self, url: str, *, params: Any = None, headers: dict | None = None, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT, stranger: bool = False,
                  hosts: frozenset[str] | None = None) -> Reply:
        """`stranger` is for a link a member supplied. It only reaches public addresses, and each redirect is checked again. `hosts`, when
        given, names the only hosts a request may go to: the first and every redirect are checked before they are sent."""
        return await self._request("GET", url, params=params, headers=headers, max_bytes=max_bytes, timeout=timeout, stranger=stranger, hosts=hosts)

    async def post(self, url: str, *, data: Any = None, headers: dict | None = None, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT) -> Reply:
        return await self._request("POST", url, data=data, headers=headers, max_bytes=max_bytes, timeout=timeout)

    async def _request(self, method: str, url: str, *, params: Any = None, data: Any = None, headers: dict | None = None, max_bytes: int, timeout: float, stranger: bool = False,
                       hosts: frozenset[str] | None = None) -> Reply:
        if self.offline:
            raise OutsideError("This copy of the bot doesn't call outside services.")
        sent = {"User-Agent": USER_AGENT, **(headers or {})}
        limit = aiohttp.ClientTimeout(total=timeout)
        session = self._session(stranger)
        by_hand = stranger or hosts is not None  # redirects followed here, one at a time, so each is checked before it is sent
        allowed = frozenset(host.lower() for host in hosts) if hosts is not None else None
        try:
            for _ in range(MAX_REDIRECTS + 1):
                if stranger:
                    check_public_link(url)
                if allowed is not None and (urlsplit(url).hostname or "") not in allowed:
                    raise OutsideError("That link leads to a site the bot doesn't fetch from.")
                async with session.request(method, url, params=params, data=data, headers=sent, timeout=limit, allow_redirects=not by_hand) as response:
                    location = response.headers.get("Location")
                    if by_hand and response.status in REDIRECTS and location:
                        url = urljoin(str(response.url), location)
                        params = None
                        continue
                    body = await self._read(response, max_bytes)
                    return Reply(response.status, body, {name.lower(): value for name, value in response.headers.items()}, str(response.url))
        except asyncio.TimeoutError as error:
            raise OutsideError("The service took too long to answer.") from error
        except aiohttp.ClientError as error:
            raise OutsideError(f"Could not reach the service: {type(error).__name__}.") from error
        except (OSError, ValueError) as error:
            raise OutsideError("Could not reach the service.") from error
        raise OutsideError("That link redirects too many times.")

    async def _read(self, response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise OutsideError("The answer was too large.")
            chunks.append(chunk)
        return b"".join(chunks)

    async def close(self) -> None:
        for session in (self._plain, self._guarded):
            if session is not None and not session.closed:
                await session.close()
        self._plain = self._guarded = None
