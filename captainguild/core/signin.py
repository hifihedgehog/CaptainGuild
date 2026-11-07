"""One-time sign-in links. Discord has already checked who ran the command, so the bot hands that person a link and the website trades it for a session.

Links live in memory and only by hash. A restart just means asking for a new one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..crypto import new_token, token_hash

LINK_SECONDS = 300
MAX_LINKS = 10_000


@dataclass(frozen=True)
class SignInLink:
    user_id: int
    name: str
    avatar: str | None
    target: str
    expires_at: float


class SignInLinks:
    def __init__(self) -> None:
        self._links: dict[str, SignInLink] = {}
        self._latest: dict[int, str] = {}

    def create(self, user_id: int, name: str, avatar: str | None, target: str, *, now: float | None = None) -> str:
        """A new link for this person. Their earlier link stops working, so only the newest one is ever live."""
        now = time.time() if now is None else now
        for key in [key for key, link in self._links.items() if link.expires_at <= now]:
            self._drop(key)
        earlier = self._latest.get(user_id)
        if earlier is not None:
            self._drop(earlier)
        while len(self._links) >= MAX_LINKS:
            self._drop(next(iter(self._links)))
        token = new_token()
        key = token_hash(token)
        self._links[key] = SignInLink(user_id, name, avatar, target, now + LINK_SECONDS)
        self._latest[user_id] = key
        return token

    def peek(self, token: str, *, now: float | None = None) -> SignInLink | None:
        """Who the link signs in, without using it up. A browser or a link scanner can fetch a page without anyone meaning to sign in."""
        link = self._links.get(token_hash(token))
        if link is None or link.expires_at <= (time.time() if now is None else now):
            return None
        return link

    def take(self, token: str, *, now: float | None = None) -> SignInLink | None:
        """Use the link up. It can't work twice."""
        link = self.peek(token, now=now)
        if link is not None:
            self._drop(token_hash(token))
        return link

    def cancel(self, token: str) -> None:
        self._drop(token_hash(token))

    def _drop(self, key: str) -> None:
        link = self._links.pop(key, None)
        if link is not None and self._latest.get(link.user_id) == key:
            del self._latest[link.user_id]
