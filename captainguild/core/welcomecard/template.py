"""Fills a picture's texts through the template language, and keeps what came from outside the template apart from what the server wrote.

The template engine moves outside text, such as a member's name, to private code points while a template runs, and turns it back at the very
end. A picture needs it kept apart for one step longer, because the marks **bold**, *italic* and __underline__ and server emoji are read
after the template has run, and a member called **Sam** must not come out bold. So each text runs inside an assignment block, which holds
the result as the engine has it, and the result is read from the run's variables before the engine makes it plain.

A brace with no partner would close that block early or leave it open, so those are swapped for stand-ins while the text runs and put back
after. Blocks that need Discord do nothing here, as in any message a server writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..tagscript import Interpreter, LimitReached, Scope
from ..tagscript.text import plain, written_by_template
from .text import Marked, server_emoji

_HOLD = "welcomecard"  # the variable each text's result is held in
_OPEN, _CLOSE = "\U000f007b", "\U000f007d"  # braces with no partner, while a text runs


@dataclass
class Filled:
    texts: dict[str, Marked] = field(default_factory=dict)   # by text layer ID
    addresses: dict[str, str] = field(default_factory=dict)  # what each picture layer's address came out as, by layer ID
    emoji: list[str] = field(default_factory=list)           # the IDs of the server emoji the texts draw, in order and each once


def _paired(source: str) -> str:
    """The text with every brace that has no partner swapped for a stand-in. The engine pairs braces the same way: a closing brace takes the
    nearest opening one, and what is left over is ordinary text."""
    letters = list(source.replace(_OPEN, "").replace(_CLOSE, ""))
    waiting: list[int] = []
    for index, letter in enumerate(letters):
        if letter == "{":
            waiting.append(index)
        elif letter == "}":
            if waiting:
                waiting.pop()
            else:
                letters[index] = _CLOSE
    for index in waiting:
        letters[index] = _OPEN
    return "".join(letters)


def _marked(held: str, *, marks: bool = True) -> Marked:
    """The engine's own form of a result as plain text, with a note of each character that came from outside the template. The engine's
    own readers decide both, so text it keeps as data for another reason, such as a date format's own words, still counts as the server's."""
    letters: list[str] = []
    outside = bytearray()
    for index, letter in enumerate(held):
        if letter in (_OPEN, _CLOSE):
            letters.append("{" if letter == _OPEN else "}")
            outside.append(1)
        else:
            letters.append(plain(letter))
            outside.append(0 if written_by_template(held, index, index + 1) else 1)
    # A post guards "](" in a name with a backslash, so the name can't make a link. A picture has no links, so the backslash goes.
    for at in range(len(letters) - 2, 0, -1):
        if letters[at - 1:at + 2] == ["]", "\\", "("] and outside[at - 1] and outside[at] and outside[at + 1]:
            del letters[at]
            del outside[at]
    start, end = 0, len(letters)
    while start < end and letters[start] in "\n ":
        start += 1
    while end > start and letters[end - 1] in "\n ":
        end -= 1
    return Marked("".join(letters[start:end]), bytes(outside[start:end]), marks)


def fill_text(run: Interpreter, source: str) -> Marked:
    """One text through the template language. Never raises: a text that asks for too much is drawn as written, the way a message goes out as written."""
    if "{" not in source:
        return Marked.of(source.strip("\n "))
    prefix = "{=(" + _HOLD + "):"
    run.variables.pop(_HOLD, None)
    try:
        left = run.render(prefix + _paired(source) + "}")
    except LimitReached:
        return Marked.of(source.strip("\n "))
    if run.body is not None:  # a break block chose the whole result
        return _marked(run.body)
    held = run.variables.pop(_HOLD, None)
    if isinstance(held, str):
        return _marked(held)
    # A stop block ended the run before the result was held. The engine has already made it plain, so nobody can say which part came
    # from outside, and none of it is read as a mark.
    return _marked(left[len(prefix):] if left.startswith(prefix) else left, marks=False)


def fill_address(run: Interpreter, source: str) -> str:
    """A picture layer's address through the template language, as plain text."""
    if "{" not in source:
        return source.strip()
    try:
        return run.render(source).strip()
    except LimitReached:
        return ""


def fill(design: dict, scope: Scope) -> Filled:
    """Every text and address of a design, in layer order, in one run, so a later layer can read what an earlier one assigned. Runs on a worker thread."""
    run = Interpreter(scope)
    out = Filled()
    for layer in design["layers"]:
        if layer["hidden"]:
            continue
        if layer["type"] == "text":
            marked = out.texts[layer["id"]] = fill_text(run, layer["text"])
            for found in server_emoji(marked):  # read here, on the worker thread, so the event loop never reads a text
                if found not in out.emoji:
                    out.emoji.append(found)
        elif layer["type"] == "picture" and layer["source"]["kind"] == "address":
            out.addresses[layer["id"]] = fill_address(run, layer["source"]["address"])
    return out
