"""Limits, and how text from outside a template stays data.

A block's shape comes from the characters around it, so text a member typed could otherwise rewrite the template it lands in. A message
holding "x==x" would pass a passphrase check, and one holding ")" would break the block it sits in. Text from outside is therefore moved
to private code points as it enters, one character for one, so the parser and the comparison operators can't see its punctuation or read
it as a block's name. Blocks that need its value turn it back with `plain`, and the final text always comes out plain.
"""
from __future__ import annotations

import re
from typing import Callable

MAX_SOURCE = 100_000     # characters in one template
MAX_VALUE = 100_000      # characters one block can produce
MAX_WORK = 8_000_000     # characters handled across one run, which stops doubling variables and deep nesting
MAX_BLOCKS = 20_000      # blocks in one template
MAX_SECONDS = 0.25       # listeners share one thread with the whole bot, so a run gets this long and no more
MAX_COMMANDS = 5         # command blocks that run per tag
MAX_REACTIONS = 5        # emoji per tag, for each of react and reactu
OUT_OF_TIME = "too much time"   # what LimitReached says when a run used all of MAX_SECONDS

_SHIFT = 0x10FF00
_FIRST, _LAST = 0x20, 0x7E
_TO_SHIELD: dict[int, int | None] = {code: _SHIFT + code for code in range(_FIRST, _LAST + 1)}
_DROP: dict[int, None] = {_SHIFT + code: None for code in range(_FIRST, _LAST + 1)}
_TO_SHIELD.update(_DROP)
_TO_PLAIN = {_SHIFT + code: code for code in range(_FIRST, _LAST + 1)}
_SHIELDED = re.compile(f"[{chr(_SHIFT + _FIRST)}-{chr(_SHIFT + _LAST)}]")
_RUNS = re.compile(f"[{chr(_SHIFT + _FIRST)}-{chr(_SHIFT + _LAST)}]+|[^{chr(_SHIFT + _FIRST)}-{chr(_SHIFT + _LAST)}]+")


class LimitReached(Exception):
    """A template asked for more work than one run may do."""


def shield(text: str) -> str:
    """Mark text from outside the template. A member can't type their way past this: the private code points themselves are removed."""
    return text.translate(_TO_SHIELD)


def plain(text: str) -> str:
    """The text as a person would read it."""
    return text if text.isascii() else text.translate(_TO_PLAIN)


def clean_source(text: str) -> str:
    """Take the private code points out of a template, where they would pass for text from outside."""
    return text.translate(_DROP) if _SHIELDED.search(text) else text


def is_shielded(text: str | None) -> bool:
    return bool(text) and not text.isascii() and _SHIELDED.search(text) is not None


def carry(result: str, *sources: str | None) -> str:
    """A block's result is as untrusted as what went into it, for results that can't be traced back character by character."""
    return shield(result) if any(is_shielded(source) for source in sources) else result


def map_runs(text: str, change: Callable[[str], str]) -> str:
    """Change text while keeping what the author wrote apart from what came from outside, so a pipe the author wrote still divides an if around it."""
    if not is_shielded(text):
        return change(text)
    return "".join(shield(change(plain(run))) if is_shielded(run) else change(run) for run in _RUNS.findall(text))


def replace_keeping(text: str, before: str, after: str) -> str:
    """str.replace with `before` read as plain text, leaving every other character of `text` and all of `after` as they came."""
    if not before:
        return after + after.join(text) + after if text else after
    reading = plain(text)
    parts, position = [], 0
    while True:
        found = reading.find(before, position)
        if found < 0:
            parts.append(text[position:])
            return "".join(parts)
        parts.append(text[position:found])
        parts.append(after)
        position = found + len(before)


def _spans(text: str, delimiter: str) -> list[tuple[int, int]]:
    spans, position = [], 0
    while True:
        found = text.find(delimiter, position)
        if found < 0:
            spans.append((position, len(text)))
            return spans
        spans.append((position, found))
        position = found + len(delimiter)


def split_keeping(text: str, delimiter: str) -> list[str]:
    """Split on a delimiter wherever it came from. Each piece keeps its characters as they were, because shielding moves one character for one."""
    reading = plain(text)
    if reading == text:
        return text.split(delimiter)
    return [text[start:end] for start, end in _spans(reading, delimiter)]


def split_list(text: str) -> list[str]:
    """The list form random, list and cycle read: tildes when there are any, otherwise commas."""
    return split_keeping(text, "~" if "~" in plain(text) else ",")


def words_of(text: str, parameter: str | None, payload: str | None) -> str:
    """A variable read with an index. 1 is the first piece, 0 the last, 3+ the third onward and +3 up to the third. Anything unusable gives the whole value."""
    if parameter is None:
        return text
    index_text = plain(parameter)
    delimiter = " " if payload is None else plain(payload)
    if not delimiter:
        return text
    reading = plain(text)
    spans = _spans(reading, delimiter)
    try:
        if "+" not in index_text:
            start, end = spans[int(index_text) - 1]
            return text[start:end]
        index = int(index_text.replace("+", "")) - 1
        if index_text.startswith("+"):
            chosen = spans[:index + 1]
        elif index_text.endswith("+"):
            chosen = spans[index:]
        else:
            start, end = spans[index]
            return text[start:end]
    except (ValueError, IndexError):
        return text
    if not chosen:
        return ""
    return text[chosen[0][0]:chosen[-1][1]]
