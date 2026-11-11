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

_SHIFT = 0x10FF00  # text from outside the template
_KEPT = 0x10FE00   # the template's own text inside a result that has to stay data as a whole, as a strf format read with a date from outside
_FIRST, _LAST = 0x20, 0x7E
_DROP: dict[int, None] = {shift + code: None for shift in (_SHIFT, _KEPT) for code in range(_FIRST, _LAST + 1)}
_TO_SHIELD: dict[int, int | None] = {**_DROP, **{code: _SHIFT + code for code in range(_FIRST, _LAST + 1)}}
_TO_KEPT: dict[int, int | None] = {**_DROP, **{code: _KEPT + code for code in range(_FIRST, _LAST + 1)}}
KEEPING: dict[int, int] = {code: _KEPT + code for code in range(_FIRST, _LAST + 1)}  # keep_as_data for text that holds no data, one character for one
SHIELDING: dict[int, int | None] = _TO_SHIELD  # the table `shield` uses, for code that marks text as it's made
_TO_PLAIN = {shift + code: code for shift in (_SHIFT, _KEPT) for code in range(_FIRST, _LAST + 1)}
_INSIDE_A_WORD = "_'\N{RIGHT SINGLE QUOTATION MARK}"  # what title_case reads as part of a word besides letters and digits
_OUTSIDE_RANGE = f"{chr(_SHIFT + _FIRST)}-{chr(_SHIFT + _LAST)}"
_KEPT_RANGE = f"{chr(_KEPT + _FIRST)}-{chr(_KEPT + _LAST)}"
_SHIELDED = re.compile(f"[{_OUTSIDE_RANGE}]")  # what came from outside
_DATA = re.compile(f"[{_OUTSIDE_RANGE}{_KEPT_RANGE}]")  # anything the parser has to read as data
_RUNS = re.compile(f"[{_OUTSIDE_RANGE}]+|[{_KEPT_RANGE}]+|[^{_OUTSIDE_RANGE}{_KEPT_RANGE}]+")


class LimitReached(Exception):
    """A template asked for more work than one run may do."""


def shield(text: str) -> str:
    """Mark text from outside the template. A member can't type their way past this: the private code points themselves are removed."""
    return text.translate(_TO_SHIELD)


def keep_as_data(text: str) -> str:
    """Mark text the template wrote so the parser reads it as data, as `shield` does, while it still counts as the template's own."""
    return text.translate(_TO_KEPT)


def plain(text: str) -> str:
    """The text as a person would read it."""
    return text if text.isascii() else text.translate(_TO_PLAIN)


def strip_marked(text: str) -> str:
    """`plain(text).strip("\\n ")` with what came from outside still marked: the same characters come off each end."""
    start, end = 0, len(text)
    while start < end and plain(text[start]) in "\n ":
        start += 1
    while end > start and plain(text[end - 1]) in "\n ":
        end -= 1
    return text[start:end]


def written_by_template(text: str, start: int, end: int) -> bool:
    """Whether every character of text[start:end] came from the template, and none from outside it."""
    return _SHIELDED.search(text, start, end) is None


def clean_source(text: str) -> str:
    """Take the private code points out of a template, where they would pass for text from outside or for the template's own data."""
    return text.translate(_DROP) if _DATA.search(text) else text


def is_shielded(text: str | None) -> bool:
    """Whether any of the text is data, from outside or kept as data by the template."""
    return bool(text) and not text.isascii() and _DATA.search(text) is not None


def carry(result: str, *sources: str | None) -> str:
    """A block's result is as untrusted as what went into it, for results that can't be traced back character by character."""
    return shield(result) if any(is_shielded(source) for source in sources) else result


def map_runs(text: str, change: Callable[[str], str]) -> str:
    """Change text while keeping what the author wrote apart from what came from outside, so a pipe the author wrote still divides an if around it."""
    if not is_shielded(text):
        return change(text)
    return "".join(_changed_run(run, change) for run in _RUNS.findall(text))


def _changed_run(run: str, change: Callable[[str], str]) -> str:
    if _SHIELDED.match(run):
        return shield(change(plain(run)))
    if _DATA.match(run):
        return keep_as_data(change(plain(run)))
    return change(run)


def _shift_of(character: str) -> int:
    """Which range a character was moved to, or 0 for one that wasn't moved."""
    code = ord(character)
    for shift in (_SHIFT, _KEPT):
        if shift + _FIRST <= code <= shift + _LAST:
            return shift
    return 0


def title_case(text: str) -> str:
    """The first letter of each word in upper case, and every other character as it came. A word is letters, digits, `_` and apostrophes, so "it's" stays one word.

    It reads through the shielding one character at a time, because a word can start in the template and end in a member's name."""
    out = []
    starts = True
    for character in text:
        shift = _shift_of(character)
        reading = chr(ord(character) - shift) if shift else character
        if starts:
            raised = reading.title()
            if len(raised) == 1:  # a letter with no single capital, such as the German sharp s, stays as written
                reading = raised
        starts = not (reading.isalnum() or reading in _INSIDE_A_WORD)
        out.append(chr(ord(reading) + shift) if shift and _FIRST <= ord(reading) <= _LAST else reading)
    return "".join(out)


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
