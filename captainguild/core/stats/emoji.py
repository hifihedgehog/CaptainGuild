"""Finding standard emoji in message text, from the same list the dashboard's emoji picker uses.

A skin tone counts as the emoji without it, and the emoji presentation selector is dropped, so each emoji has one key. A character that
is text by default, such as the copyright sign, counts only when the selector or a skin tone after it asks for the emoji.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LIST_FILE = Path(__file__).resolve().parent.parent.parent / "web" / "static" / "vendor" / "emoji.json"
SELECTOR = "\N{VARIATION SELECTOR-16}"
TONES = frozenset(chr(code) for code in range(0x1F3FB, 0x1F400))


@lru_cache(maxsize=1)
def _table() -> tuple[frozenset[str], frozenset[str], int]:
    """(every emoji without selectors, the single characters that need one to count, the longest emoji in characters)."""
    groups = json.loads(LIST_FILE.read_text(encoding="utf-8"))
    bare: set[str] = set()
    needs_selector: set[str] = set()
    for group in groups:
        for entry in group["emojis"]:
            text = entry[0]
            plain = text.replace(SELECTOR, "")
            bare.add(plain)
            if len(plain) == 1 and SELECTOR in text:
                needs_selector.add(plain)
    return frozenset(bare), frozenset(needs_selector), max(len(item) for item in bare)


def find(text: str) -> set[str]:
    """The standard emoji in some text, each once."""
    if not text or text.isascii():
        return set()
    bare, needs_selector, longest = _table()
    chars: list[str] = []
    shown: list[bool] = []  # whether a selector or a skin tone followed the character
    for character in text:
        if character == SELECTOR or character in TONES:
            if shown:
                shown[-1] = True
            continue
        chars.append(character)
        shown.append(False)
    found: set[str] = set()
    index = 0
    while index < len(chars):
        for length in range(min(longest, len(chars) - index), 0, -1):
            piece = "".join(chars[index:index + length])
            if piece in bare and not (length == 1 and piece in needs_selector and not shown[index]):
                found.add(piece)
                index += length
                break
        else:
            index += 1
    return found


def bare(text: str) -> str:
    """An emoji without its presentation selector and skin tone, as reactions and the picker send it, so each emoji has one key."""
    return "".join(character for character in text if character != SELECTOR and character not in TONES)


def display(key_text: str) -> str:
    """An emoji key's characters ready to show, with the selector back on a character that needs it."""
    _, needs_selector, _ = _table()
    return key_text + SELECTOR if key_text in needs_selector else key_text
