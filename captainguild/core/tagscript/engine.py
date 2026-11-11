"""The interpreter for the template language.

A template is text holding blocks in curly braces: {name(parameter):payload}. Braces are matched once, on the text as written. Blocks then run from the
inside out and left to right, and each result replaces its block in the text around it, so an outer block reads what its inner blocks produced. Every
complete block runs, even one in the branch of an if that isn't taken. Building a tree first keeps each step's cost to the text it touches, and walking it
without recursion means that nesting can't overflow Python's stack.

A run keeps its variables across calls to `render`, so a message's embed can read what its text assigned.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import objects
from .blocks import BLOCKS, GIVES_WAY
from .text import MAX_BLOCKS, MAX_SECONDS, MAX_SOURCE, MAX_VALUE, MAX_WORK, OUT_OF_TIME, LimitReached, clean_source, plain, shield, strip_marked, words_of

_BRACE = re.compile(r"[{}]")
_SHAPE = re.compile(r"[:()]")


@dataclass
class Scope:
    """What a template can see. Any of these can be None. `extra` adds variables such as {reason}: text, or a thing from objects.py with properties of its own."""
    guild: Any = None
    member: Any = None
    channel: Any = None
    target: Any = None
    args: str = ""
    uses: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    role: Any = None             # the role that changed, for a role trigger
    role_added: bool | None = None
    now: float | None = None     # the clock a run reads, when a caller needs it fixed
    acting_member: Any = None    # who a run acts for when that isn't `member`: a role trigger's {user} is whoever changed the role, and its blocks act for the member it changed on


@dataclass
class Actions:
    """What the blocks asked for. Nothing here has happened yet: run_tag carries it out, and render_text ignores it."""
    delete: bool = False
    silent: bool = False
    override: bool = False
    destination: str | None = None   # "dm" or "channel". The last block to ask decides.
    channel_text: str | None = None
    requires: list[tuple[str | None, list[str]]] = field(default_factory=list)
    blacklists: list[tuple[str | None, list[str]]] = field(default_factory=list)
    react: list[str] = field(default_factory=list)
    react_up: list[str] = field(default_factory=list)
    embed: list[tuple[str, str]] = field(default_factory=list)
    commands: list[tuple[str, bool]] = field(default_factory=list)   # (text, at the bot's own level)


class _Node:
    __slots__ = ("start", "end", "parent", "children", "output")

    def __init__(self, start: int) -> None:
        self.start = start
        self.end = 0
        self.parent: _Node | None = None
        self.children: list[_Node] = []
        self.output = ""


def _parse(source: str) -> tuple[_Node, list[_Node]]:
    """The tree of matched braces, and its blocks in the order they run: by closing brace."""
    root = _Node(0)
    root.end = len(source) - 1
    stack = [root]
    order: list[_Node] = []
    for match in _BRACE.finditer(source):
        if match.group() == "{":
            stack.append(_Node(match.start()))
        elif len(stack) > 1:
            node = stack.pop()
            node.end = match.start()
            node.parent = stack[-1]
            stack[-1].children.append(node)
            order.append(node)
    # A brace that never closed is ordinary text, and what closed inside it belongs to the level above.
    while len(stack) > 1:
        open_brace = stack.pop()
        for child in open_brace.children:
            child.parent = stack[-1]
        stack[-1].children.extend(open_brace.children)
    return root, order


def _text_of(source: str, node: _Node) -> str:
    """A node's text as it stands now, with its finished children in place."""
    if not node.children:
        return source[node.start:node.end + 1]
    parts = []
    position = node.start
    for child in node.children:
        parts.append(source[position:child.start])
        parts.append(child.output)
        position = child.end + 1
    parts.append(source[position:node.end + 1])
    return "".join(parts)


def _text_before(source: str, node: _Node) -> str:
    """Everything before a node in the whole template, as it stands now."""
    levels = []
    current = node
    while current.parent is not None:
        parent = current.parent
        parts = []
        position = parent.start
        for child in parent.children:
            if child is current:
                break
            parts.append(source[position:child.start])
            parts.append(child.output)
            position = child.end + 1
        parts.append(source[position:current.start])
        levels.append("".join(parts))
        current = parent
    return "".join(reversed(levels))


def split_block(text: str) -> tuple[str, str | None, str | None]:
    """Name, parameter and payload of a block's text, braces included. Only punctuation from the template shapes it."""
    inner = text[1:-1]
    depth = 0
    start = 0
    for match in _SHAPE.finditer(inner):
        position = match.start()
        mark = match.group()
        if mark == ":":
            if depth == 0:
                return inner[:position], None, inner[position + 1:]
        elif mark == "(":
            if not start:
                start = position
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                after = position + 1
                payload = inner[after + 1:] if inner[after:after + 1] == ":" else None
                return inner[:start], inner[start + 1:position], payload
    head, colon, tail = inner.partition(":")
    return head, None, tail if colon else None


class Interpreter:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.now = scope.now if scope.now is not None else time.time()
        self.actions = Actions()
        self.variables: dict[str, Any] = {}
        self.body: str | None = None
        self.stopped = False
        self.exhausted = False
        self._work = 0
        self._deadline = time.thread_time() + MAX_SECONDS  # processor time, since a run on a worker thread also waits its turn while the rest of the bot is busy, and that isn't the template's doing
        self._seed()

    def _seed(self) -> None:
        scope = self.scope
        args = shield(str(scope.args or ""))
        self.variables.update({"args": args, "message": args, "uses": str(scope.uses or 0), "unix": str(int(self.now))})
        if scope.member is not None:
            self.variables["user"] = objects.person(scope.member)
            self.variables["mention"] = shield(str(getattr(scope.member, "mention", "")))
        target = scope.target if scope.target is not None else scope.member
        if target is not None:
            self.variables["target"] = objects.person(target)
        if scope.guild is not None:
            self.variables["server"] = objects.server(scope.guild)
        if scope.channel is not None:
            self.variables["channel"] = objects.channel(scope.channel)
        if scope.role is not None:
            self.variables["role"] = objects.role(scope.role, scope.role_added)
        for name, value in (scope.extra or {}).items():
            # A feature can hand over a thing with properties, such as the invite a greeting reads. Anything else is text from outside.
            self.variables[str(name)] = value if isinstance(value, objects.Thing) else shield("" if value is None else str(value))

    def _spend(self, characters: int) -> None:
        self._work += characters
        if self._work > MAX_WORK:
            raise LimitReached("too much text")
        if time.thread_time() > self._deadline:
            raise LimitReached(OUT_OF_TIME)

    def _run_block(self, text: str) -> str | None:
        head, parameter, payload = split_block(text)
        name = plain(head)
        lowered = name.lower()  # once, since a block's name can be all of a long text
        # A name built from a member's text only ever reads a variable, or {{args}} would let them run any block. A variable named like a
        # block newer than it reads as that variable.
        if name == head and not (lowered in GIVES_WAY and name in self.variables):
            block = BLOCKS.get(lowered)
            if block is not None:
                value = block(self, lowered, parameter, payload)
                if value is not None:
                    return value
        return self._read_variable(name, parameter, payload)

    def _read_variable(self, name: str, parameter: str | None, payload: str | None) -> str | None:
        if name.isdigit():  # {2} is the second word of {message}
            value: Any = self.variables.get("message")
            parameter, payload = name, None
        else:
            value = self.variables.get(name)
        if value is None:
            return None
        if isinstance(value, str):
            if parameter is not None:
                self._spend(len(value))  # finding the pieces walks the whole value
            return words_of(value, parameter, payload)
        return value.read(parameter, payload)

    def render(self, source: str) -> str:
        """Run one piece of text. Raises LimitReached when it asks for too much, and any variables it set stay set."""
        return plain(self._result(source)).strip("\n ")

    def render_marked(self, source: str) -> str:
        """`render`, with every character that came from outside the template still marked (see text.py), so a caller can tell what the template wrote."""
        return strip_marked(self._result(source))

    def _result(self, source: str) -> str:
        if len(source) > MAX_SOURCE:
            raise LimitReached("the template is too long")
        source = clean_source(source)
        root, order = _parse(source)
        if len(order) > MAX_BLOCKS:
            raise LimitReached("too many blocks")
        self.body = None
        self.stopped = False
        result = None
        for node in order:
            text = _text_of(source, node)
            self._spend(len(text))
            value = self._run_block(text)
            if value is None:
                node.output = text
            else:
                if len(value) > MAX_VALUE:
                    raise LimitReached("a block produced too much text")
                self._spend(len(value))
                node.output = value
            if self.stopped:
                result = _text_before(source, node) + node.output
                break
        if self.body is not None:
            result = self.body
        elif result is None:
            result = _text_of(source, root)
        if len(result) > MAX_VALUE:
            raise LimitReached("the output is too long")
        return result

    def render_or_keep_marked(self, text: str) -> tuple[str, str]:
        """`render_or_keep`, and beside it the same text with every character that came from outside the template still marked. The two are the same length."""
        if self.exhausted or "{" not in text:
            return text, text
        try:
            marked = self.render_marked(text)
        except LimitReached:
            self.exhausted = True
            return text, text
        return plain(marked), marked

    def render_or_keep(self, text: str) -> str:
        """Run one piece of text, leaving it as written when it asks for too much. The budget is per run, so what's left of a message stays as written too."""
        if self.exhausted or "{" not in text:
            return text
        try:
            return self.render(text)
        except LimitReached:
            self.exhausted = True
            return text
