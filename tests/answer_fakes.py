"""A scripted AI service for the Wiki answers tests, reached through the real Anthropic SDK with only the network replaced.

    model = ScriptedModel()
    model.reply(model.tool("search_wiki", {"query": "nozzle"}))
    model.reply(model.text("Heat it first. [S1]"))
    cog.client = model.client()

Each request takes the next reply in order and is kept, as the JSON the SDK sent, in `model.requests`. A reply can be a message, an
HTTP error from `model.error(...)`, an error partway through a stream from `model.broken(...)`, or an exception to raise as a failed
connection. A request that asks to stream gets the message as the service streams one: message_start, each block opened, filled by its
deltas and closed, then message_delta with the stop reason and the usage totals, and message_stop.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2

_ids = itertools.count(1)


class ScriptedModel:
    def __init__(self) -> None:
        self.replies: list[Any] = []
        self.requests: list[dict] = []
        self.headers: list[dict] = []

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(api_key="test-key", max_retries=0, http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self._answer)))

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if not self.replies:
            raise AssertionError("The model was asked more often than the test expected")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, data = reply
        if status == "broken":
            opening = self.message([], "end_turn", input_tokens=0, output_tokens=0)
            return httpx2.Response(200, headers=_EVENTS, content=(_event("message_start", {"type": "message_start", "message": {**opening, "model": body["model"], "stop_reason": None}})
                                                                  + _event("error", data)).encode())
        if status == 200:
            data = {**data, "model": body["model"]}
            if body.get("stream"):
                return httpx2.Response(200, headers=_EVENTS, content=_streamed(data).encode())
        return httpx2.Response(status, json=data)

    def reply(self, *replies: Any) -> "ScriptedModel":
        for reply in replies:
            self.replies.append(reply if isinstance(reply, (tuple, Exception)) else (200, reply))
        return self

    @staticmethod
    def message(content: list[dict], stop_reason: str = "end_turn", *, input_tokens: int = 1000, output_tokens: int = 100, cache_write: int = 0,
                cache_read: int = 0, web_searches: int = 0) -> dict:
        usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_creation_input_tokens": cache_write, "cache_read_input_tokens": cache_read}
        if web_searches:
            usage["server_tool_use"] = {"web_search_requests": web_searches}
        return {"id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": "", "content": content, "stop_reason": stop_reason,
                "stop_sequence": None, "usage": usage}

    def text(self, words: str, *, stop_reason: str = "end_turn", **usage: Any) -> dict:
        return self.message([{"type": "text", "text": words}], stop_reason, **usage)

    def tool(self, name: str, args: dict, *, words: str = "", **usage: Any) -> dict:
        content: list[dict] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
        if words:
            content.append({"type": "text", "text": words})
        content.append({"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args})
        return self.message(content, "tool_use", **usage)

    @staticmethod
    def error(status: int, kind: str, words: str) -> tuple[int, dict]:
        return status, {"type": "error", "error": {"type": kind, "message": words}}

    @staticmethod
    def broken(kind: str, words: str) -> tuple[str, dict]:
        """A stream that opens and then sends an error event, as the service does when it fails partway through an answer."""
        return "broken", {"type": "error", "error": {"type": kind, "message": words}}

    def tool_results(self, request: int) -> list[dict]:
        """The tool results the bot sent with one request, by its number."""
        last = self.requests[request]["messages"][-1]
        return [part for part in last["content"] if isinstance(part, dict) and part.get("type") == "tool_result"] if isinstance(last["content"], list) else []


_EVENTS = {"content-type": "text/event-stream"}


def _event(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def _streamed(message: dict) -> str:
    """The message as the service streams it. Text and thinking arrive as deltas, and a tool's input as JSON in pieces, from blocks that
    open empty. Other blocks, such as web search results, arrive whole when they open."""
    usage = message["usage"]
    events = [_event("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None, "stop_sequence": None,
                                                                             "usage": {**usage, "output_tokens": 1}}})]
    for index, block in enumerate(message["content"]):
        kind = block["type"]
        if kind == "text":
            opened, deltas = {**block, "text": ""}, [{"type": "text_delta", "text": block["text"]}]
        elif kind == "thinking":
            opened = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [{"type": "thinking_delta", "thinking": block.get("thinking", "")}, {"type": "signature_delta", "signature": block.get("signature", "")}]
        elif kind in ("tool_use", "server_tool_use"):
            serialized = json.dumps(block.get("input") or {})
            opened = {**block, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": serialized[:len(serialized) // 2]}, {"type": "input_json_delta", "partial_json": serialized[len(serialized) // 2:]}]
        else:
            opened, deltas = block, []
        events.append(_event("content_block_start", {"type": "content_block_start", "index": index, "content_block": opened}))
        events += [_event("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}) for delta in deltas]
        events.append(_event("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"], "stop_sequence": message.get("stop_sequence")}, "usage": usage}))
    events.append(_event("message_stop", {"type": "message_stop"}))
    return "".join(events)


# A term-matching module of the shape core/answers/screening.py takes, matching plain words. Its character map reads 0 as o.
TERM_MODULE = '''
import re

character_map = {}
pattern_cache = {}


async def load_character_map():
    character_map.update({"0": "o"})


async def replacement_string(config):
    return config.get("replacement_string", "***")


def _terms(blacklists, whitelists):
    allowed = {term.lower() for terms in whitelists.values() for term in terms}
    return [term for terms in blacklists.values() for term in terms if term.lower() not in allowed]


def _plain(content):
    return "".join(character_map.get(letter, letter) for letter in content.lower())


async def find_blocked_terms(content, blacklists, whitelists):
    found = set()
    for term in _terms(blacklists, whitelists):
        pattern_cache.setdefault(term, re.compile(re.escape(term), re.I))
        if pattern_cache[term].search(_plain(content)):
            found.add(term)
    return found


async def censor_terms(content, blacklists, whitelists, replacement):
    for term in _terms(blacklists, whitelists):
        content = re.sub(re.escape(term), replacement, content, flags=re.I)
    return content
'''


def write_term_lists(folder: Path, configs: dict[int, dict]) -> tuple[Path, Path]:
    """The module above and a folder of server configs, as files. Returns (module path, config folder)."""
    module = folder / "term_matching.py"
    module.write_text(TERM_MODULE, encoding="utf-8")
    configs_folder = folder / "server_configs"
    configs_folder.mkdir(exist_ok=True)
    for guild_id, config in configs.items():
        (configs_folder / f"{guild_id}.json").write_text(json.dumps(config), encoding="utf-8")
    return module, configs_folder


def page_html(title: str, body: str, *, site: str = "Sample Wiki") -> str:
    """A page the way Wiki.js serves one."""
    return (f"<html><head><title>{title} | {site}</title></head><body><div id=\"root\"><page title=\"{title}\" locale=\"en\">"
            f"<template slot=\"contents\">{body}</template></page></div></body></html>")


def sitemap(base: str, pages: dict[str, str | None]) -> str:
    entries = "".join(f"<url><loc>{base}{path}</loc>{f'<lastmod>{lastmod}</lastmod>' if lastmod else ''}</url>" for path, lastmod in pages.items())
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
