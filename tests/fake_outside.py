"""A stand-in for core.outside.Outside that answers from a table, so tests of outside services never reach the internet.

    web = FakeOutside()
    web.answer("https://api.twitch.tv/helix/streams", {"data": []})
    cog.outside = web

The newest answer whose address starts the called URL wins. An address with no answer behaves like a service that's down.
"""
from __future__ import annotations

import json
from typing import Any

from captainguild.core.outside import OutsideError, Reply


def reply(data: Any = None, status: int = 200, headers: dict | None = None) -> Reply:
    """Bytes and text go out as they are, and anything else as JSON."""
    if isinstance(data, bytes):
        body = data
    elif isinstance(data, str):
        body = data.encode()
    else:
        body = json.dumps(data).encode()
    return Reply(status, body, {name.lower(): value for name, value in (headers or {}).items()})


class FakeOutside:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any]] = []
        self.calls: list[dict] = []

    def answer(self, prefix: str, answer: Any, *, status: int = 200, headers: dict | None = None) -> "FakeOutside":
        """`answer` is data, a Reply, an exception to raise, or a function taking the call and returning any of those."""
        self.routes.insert(0, (prefix, answer if isinstance(answer, (Reply, Exception)) or callable(answer) else reply(answer, status, headers)))
        return self

    def called(self, prefix: str) -> list[dict]:
        return [call for call in self.calls if call["url"].startswith(prefix)]

    async def _call(self, method: str, url: str, **kwargs: Any) -> Reply:
        call = {"method": method, "url": url, **kwargs}
        self.calls.append(call)
        for prefix, answer in self.routes:
            if url.startswith(prefix):
                if callable(answer):
                    answer = answer(call)
                if isinstance(answer, Exception):
                    raise answer
                return answer if isinstance(answer, Reply) else reply(answer)
        raise OutsideError(f"nothing answers {url}")

    async def get(self, url: str, **kwargs: Any) -> Reply:
        return await self._call("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Reply:
        return await self._call("POST", url, **kwargs)

    async def close(self) -> None:
        pass
