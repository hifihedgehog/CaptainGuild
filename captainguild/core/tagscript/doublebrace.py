"""Double-brace tags turned into the template language.

Some bots write a welcome message with tags in double braces: {{User.Mention}}, {{Ordinal(Guild.Members)}}, {{#User.Bot}}...{{/User.Bot}}.
A tag holds an expression, which can be a value such as Guild.Members, a function call, a comparison, arithmetic or a choice.
`convert_text` rewrites one piece of text into the template language, and `convert_message` does a whole Discord message.

Both return what they made and a list of notes for the person importing. A tag with no match here stays in the text as written and
gets a note that names it. Nothing is left out without a note.

Four things keep the result reading as the text did before:

  - In picture text, a section, closing or comment tag alone on its line takes the line with it, and in a message it doesn't, as there. That
    bot rendered a message's stored JSON, where a line break is written as a backslash and an n, so no tag stood alone on a line. The same
    JSON put a backslash before each double quote and each backslash, so text in quotes reads in a message as it reached that bot.
  - That bot's == and != counted values of two kinds as different, and each of its functions took one kind of value. A tag that compares
    a number with text answers as it did there, and one that gives a function the wrong kind stays as written. Each gets a note.
  - Plain text the template language would read as a block is written so it stays plain text. A brace becomes {substr(0-1):{.}}, which
    gives one brace, because a block's result is never read again.
  - An if block divides its payload at a pipe, so text holding a pipe is assigned to a variable first and read back by name.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from .. import messages
from ..validate import Invalid, hex_color, http_url
from . import objects
from .engine import Interpreter, Scope
from .numerals import FORMATS
from .payload import read_json_text
from .text import LimitReached

MAX_TEXT = 100_000   # characters one piece of text can hold
MAX_TAGS = 2_000     # tags in one piece of text
MAX_DEPTH = 20       # sections inside sections, and parentheses inside parentheses
MAX_WORDS = 200      # values and signs in one tag
MAX_PROBES = 400     # brace pairs one conversion call asks the engine about, across every part of a message
MAX_SECONDS = 1.0    # processor time one conversion call spends asking. Past it, or past MAX_PROBES, a pair is written out as braces without asking.
MAX_PROBED = 2_000   # characters in a pair that's worth trying. A longer one is written out as braces without asking.
# {.} is a name nothing answers to, so it stays as written, and substr takes its first character or its last.
OPEN_BRACE, CLOSE_BRACE = "{substr(0-1):{.}}", "{substr(2):{.}}"
BRACE_NOTE = f"Braces the template language would read as a block are written as `{OPEN_BRACE}` and `{CLOSE_BRACE}`, which show as an opening and a closing brace."
LOCALES = {"default": "plain", **{name: name for name in FORMATS}}
LINK_MAX = 2_048     # the longest link a stored greeting takes

_STANDS_ALONE = "#^/<>=!"  # the tags that take their whole line when nothing else is on it
_FLIPPED = {"==": "!=", "!=": "==", ">": "<=", "<=": ">", "<": ">=", ">=": "<"}


def _tokens(quoted: str) -> re.Pattern:
    return re.compile(
        r"\s*(?:(?P<number>\d+(?:\.\d+)?|\.\d+)|(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)|(?P<string>" + quoted + ")"
        r"|(?P<sign>&&|\|\||==|!=|>=|<=|=~|!~|\*\*|<<|>>|\?\?|[-+*/%<>!?:,()&|^~]))"
    )


# Text in quotes as the other engine read it. In picture text either quote mark opens it and either one ends it, and a backslash keeps the
# character after it. A message reached that engine as its stored JSON, with a backslash before each double quote and each backslash, which
# the engine's own escape took off again. So in a message only a single quote ends the text and a backslash inside is a plain character,
# and a double quote that opens text left a backslash the engine couldn't read. That last one is read here as it was meant, with a note.
_TOKEN = _tokens(r"""['"](?:[^'"\\]|\\.)*['"]""")
_TOKEN_IN_MESSAGE = _tokens("'[^']*'|\"[^\"]*\"")
# How the other engine's time formats begin. Text in quotes that held a real date or time in one of them it read as a count of seconds.
_DATED = re.compile(r"\d{4}-\d\d-\d\d|\d{1,2}:\d\d[AP]M|(?i:(?:mon|tue|wed|thu|fri|sat|sun) +(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))")
_CONTROL = re.compile(r"[\x00-\x1f]")
# The kinds of value the other engine told apart, as a note names them.
_KINDS = {"text": "text", "number": "a number", "flag": "a true-or-false value", "ID": "an ID", "time": "a time", "thing": "a whole person, server or invite",
          "mixed": "a choice that can give two kinds of value", "maybe": "a choice with no `:` part", "dated": "text written as a date or a time"}
_DECIMAL = "is worked out by the math block, which writes this kind of result with a decimal point, so 8/2 shows as 4.0."
_QUOTED_TWICE = ("holds text in double quotes. The other bot stored a message with a backslash before each double quote and couldn't read the tag, "
                 "so the message didn't go out whenever it came to this tag.")
_ESCAPED = re.compile(r"\\(.)", re.DOTALL)
# What a condition's own punctuation is made of. Text holding any of it can't sit inside one.
_BREAKS_A_CONDITION = re.compile(r"[|(){}=!<>]|^\s|\s$")
# The date tags a time format takes, longest first, and the strf code each becomes.
_DATE_TAGS = (
    ("yyyy", "%Y"), ("yy", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("M", "%-m"), ("dddd", "%A"), ("ddd", "%a"), ("dd", "%d"), ("d", "%-d"),
    ("HH", "%H"), ("hh", "%I"), ("h", "%-I"), ("mm", "%M"), ("m", "%-M"), ("ss", "%S"), ("s", "%-S"),
)
_URL = http_url()
_OWN_NAME = re.compile(r"text(\d+)")  # how the converter names its variables, and so the names it keeps clear of. A name is read back as written, capitals included.


class _NoMatch(Exception):
    """A tag the template language has nothing for. The text is the end of a note: what the tag is, after its own words."""


class _Parts:
    """Converted text in pieces: what the converter wrote, and what the person wrote, which has to come out as plain text."""
    __slots__ = ("items", "pipes")

    def __init__(self) -> None:
        self.items: list[tuple[str, bool]] = []
        self.pipes = False  # whether this can put a pipe into the payload of a block around it

    def written(self, text: str) -> "_Parts":
        if text:
            self.items.append((text, False))
        return self

    def plain(self, text: str) -> "_Parts":
        if text:
            self.items.append((text, True))
            self.pipes = self.pipes or "|" in text
        return self

    def add(self, other: "_Parts") -> "_Parts":
        self.items.extend(other.items)
        self.pipes = self.pipes or other.pipes
        return self

    def flat(self) -> str | None:
        """All of it as text, when none of it is the person's own. A condition can hold that as it is."""
        if any(own for _, own in self.items):
            return None
        return "".join(text for text, _ in self.items)


def _written(text: str) -> _Parts:
    return _Parts().written(text)


@dataclass
class _Test:
    """A condition: comparisons for an if, all or any block. `flipped` means the text belongs in the else part."""
    terms: list[tuple[str, str, str]]
    mode: str = "if"
    flipped: bool = False

    def head(self) -> str:
        return f"{self.mode}({'|'.join(left + sign + right for left, sign, right in self.terms)})"

    def negated(self) -> "_Test":
        if self.mode == "if" and not self.flipped:
            left, sign, right = self.terms[0]
            return _Test([(left, _FLIPPED[sign], right)])
        return _Test(self.terms, self.mode, not self.flipped)

    def term(self) -> tuple[str, str, str]:
        """This whole test as one comparison, to stand beside others."""
        if self.mode == "if" and not self.flipped:
            return self.terms[0]
        yes, no = ("false", "true") if self.flipped else ("true", "false")
        return "{" + self.head() + f":{yes}|{no}" + "}", "==", "true"


def _joined(mode: str, first: _Test, second: _Test) -> _Test:
    terms: list[tuple[str, str, str]] = []
    for test in (first, second):
        if test.mode == mode and not test.flipped:
            terms.extend(test.terms)
        else:
            terms.append(test.term())
    return _Test(terms, mode)


@dataclass
class _Value:
    """What an expression comes to. `kind` is text, number, flag, time or thing."""
    kind: str
    parts: _Parts | None = None  # what it prints. None for a thing with nothing to print
    math: str | None = None      # a number, as the math block reads it
    whole: bool = False          # a number that's always whole
    date: str | None = None      # a time, as the date the strf, td and since blocks read
    test: _Test | None = None    # a flag, as a condition
    literal: str | None = None   # a quoted string, as written
    prefix: str | None = None    # a thing: the name its fields hang from
    held: str | None = None      # the kind the other engine held, where it isn't `kind`: an ID, or a choice's


def _held(value: _Value) -> str:
    """The kind of value the other engine held for this. Its == and != count values of two kinds as different, and each function takes one kind."""
    return value.held or value.kind


def _dated(text: str) -> str:
    return (f"holds `{_shown(text)}` in quotes, written as a date or a time. The other bot read a real date or time in quotes as a count of "
            "seconds, taking it in the time zone of the computer it ran on")


def _text(template: str) -> Callable[[], _Value]:
    return lambda: _Value("text", _written(template))


def _number(template: str) -> Callable[[], _Value]:
    return lambda: _Value("number", _written(template), math=template, whole=True)


def _id(template: str) -> Callable[[], _Value]:
    return lambda: _Value("text", _written(template), held="ID")  # shown as its digits, and never equal to text or a number there


def _flag(template: str) -> Callable[[], _Value]:
    return lambda: _Value("flag", _written(template), test=_Test([(template, "==", "true")]))


def _time(shown: str, date: str) -> Callable[[], _Value]:
    return lambda: _Value("time", _written(shown), date=date)


def _thing(prefix: str, shown: str | None) -> Callable[[], _Value]:
    return lambda: _Value("thing", _written(shown) if shown else None, prefix=prefix)


# Every value a tag can name, and the template text that gives the same thing. A person's name there is the one on their own profile, never
# a server nickname, and their avatar the same, so those two read the profile properties and not {user} or {user(avatar)}.
_NAMES: dict[str, Callable[[], _Value]] = {
    "User": _thing("User", "{user(profile_name)}"),
    "User.ID": _id("{user(id)}"),
    "User.Name": _text("{user(profile_name)}"),
    "User.Username": _text("{user(name)}"),
    "User.Discriminator": _text("{user(discriminator)}"),
    "User.GlobalName": _text("{user(global_name)}"),
    "User.Mention": _text("{mention}"),
    "User.Avatar": _text("{user(profile_avatar)}"),
    "User.CreatedAt": _time("{user(created_ago)}", "{user(created_at)}"),
    "User.JoinedAt": _time("{user(joined_ago)}", "{user(joined_at)}"),
    "User.LeftAt": _time("<t:{unix}:R>", "{unix}"),  # a goodbye goes out as they leave, so the time they left is now
    "User.Bot": _flag("{user(bot)}"),
    "User.Pending": _flag("{user(pending)}"),
    "Guild": _thing("Guild", "{server}"),
    "Guild.ID": _id("{server(id)}"),
    "Guild.Name": _text("{server}"),
    "Guild.Icon": _text("{server(icon)}"),
    "Guild.Splash": _text("{server(splash)}"),
    "Guild.Banner": _text("{server(banner)}"),
    "Guild.Members": _number("{server(members)}"),
    "Guild.MembersJoined": _number("{joined}"),
    "Invite": _thing("Invite", None),
    "Invite.Code": _text("{invite}"),
    "Invite.Uses": _number("{invite(uses)}"),
    "Invite.MaxUses": _number("{invite(max_uses)}"),
    "Invite.MaxAge": _number("{invite(max_age)}"),
    "Invite.Temporary": _flag("{invite(temporary)}"),
    "Invite.ChannelID": _id("{invite(channel)}"),
    "Invite.CreatedAt": _time("{invite(created_ago)}", "{invite(created_at)}"),
    "Invite.ExpiresAt": _time("{invite(expires_in)}", "{invite(expires_at)}"),
    "Invite.Inviter": _thing("Invite.Inviter", "{invite(inviter)}"),
    "Invite.Inviter.Name": _text("{invite(inviter)}"),
    "Invite.Inviter.Username": _text("{invite(inviter_username)}"),
    "Invite.Inviter.GlobalName": _text("{invite(inviter_global_name)}"),
    "Invite.Inviter.Mention": _text("{invite(inviter_mention)}"),
    "Invite.Inviter.ID": _id("{invite(inviter_id)}"),
}


# ---- reading the text: tags, sections and the lines they take ----

@dataclass
class _Node:
    kind: str                       # "text", "tag", "section" or "kept"
    text: str = ""                  # the text, the tag's expression or the section's
    raw: str = ""                   # a tag as written, braces included
    inverted: bool = False
    children: "list[_Node] | None" = None
    line: str = ""                  # a section's opening tag with whatever of its line it took, for when it has to stay as written
    closing: str = ""               # its closing tag, the same way


def _read(text: str, notes: list[str], take_lines: bool = True) -> list[_Node]:
    """The text as a tree of plain text, tags and sections. What can't be read as a tag stays, with a note. `take_lines` is off in a message."""
    opener, closer = "{{", "}}"
    root: list[_Node] = []
    stack: list[_Node] = []
    position = 0
    tags = 0

    def add(node: _Node) -> None:
        (stack[-1].children if stack else root).append(node)

    def plain(value: str) -> None:
        if value:
            add(_Node("text", value))

    while True:
        start = text.find(opener, position)
        if start < 0:
            plain(text[position:])
            break
        tags += 1
        if tags > MAX_TAGS:
            raise Invalid(f"This holds more than {MAX_TAGS} tags, which is more than one message converts.")
        # Spaces and tabs before a tag belong to it when they start the line. Whether the tag takes the line is decided once it has been read.
        lead = start
        while lead > position and text[lead - 1] in " \t":
            lead -= 1
        may_stand = take_lines and (lead == 0 or text[lead - 1] == "\n")
        inside = start + len(opener)
        triple = text.startswith("{", inside)
        ending = ("}" + closer) if triple else closer
        end = text.find(ending, inside)
        if end < 0:
            notes.append(f"`{_shown(text[start:])}` opens a tag that never closes, so it stays as written.")
            plain(text[position:])
            break
        after = end + len(ending)
        raw = text[start:after]
        tag = text[inside:end + 1 if triple else end].strip()
        stands = False
        if may_stand and tag[:1] and tag[0] in _STANDS_ALONE:
            rest = after
            while rest < len(text) and text[rest] in " \t":
                rest += 1
            if rest == len(text):
                # Spaces after the last tag keep it from taking its line. Only a tag that ends the text does.
                stands, resume = after == len(text), rest
            elif text[rest] == "\n":
                stands, resume = True, rest + 1
            elif text.startswith("\r\n", rest):
                stands, resume = True, rest + 2
        plain(text[position:lead] if stands else text[position:start])
        position = resume if stands else after
        whole = text[lead:position] if stands else raw  # what goes back when the tag has to stay as written: the tag and the line it took
        if not tag:
            notes.append("An empty tag, `{{}}`, stays as written.")
            add(_Node("kept", raw=whole))
        elif tag[0] == "!":
            notes.append(f"The comment `{_shown(raw)}` was left out. It never showed in a message.")
        elif tag[0] in "#^":
            if len(stack) >= MAX_DEPTH:
                notes.append(f"`{_shown(raw)}` sits more than {MAX_DEPTH} sections deep, so it stays as written.")
                add(_Node("kept", raw=whole))
                continue
            node = _Node("section", tag[1:].strip(), raw, tag[0] == "^", [], whole)
            add(node)
            stack.append(node)
        elif tag[0] == "/":
            if stack and stack[-1].text == tag[1:].strip():
                stack.pop().closing = whole
            else:
                notes.append(f"`{_shown(raw)}` closes a section that isn't open there, so it stays as written.")
                add(_Node("kept", raw=whole))
        elif tag[0] == "=":
            marks = tag[1:-1].split() if tag.endswith("=") and len(tag) > 1 else []
            if len(marks) == 2:
                opener, closer = marks
            else:
                notes.append(f"`{_shown(raw)}` doesn't name two new tag marks, so it stays as written.")
                add(_Node("kept", raw=whole))
        elif tag[0] == ">":
            notes.append(f"`{_shown(raw)}` pulls in another template by name. There is none to pull in, so it stays as written.")
            add(_Node("kept", raw=whole))
        elif tag[0] == "{":
            if tag.endswith("}") and tag[1:-1].strip():
                add(_Node("tag", tag[1:-1].strip(), raw))
            else:
                notes.append(f"`{_shown(raw)}` isn't a whole tag, so it stays as written.")
                add(_Node("kept", raw=raw))
        elif tag[0] == "&":
            add(_Node("tag", tag[1:].strip(), raw))
        else:
            add(_Node("tag", tag, raw))
    while stack:
        # A section left open: its opening tag stays as written, and what followed it is ordinary text again.
        node = stack.pop()
        notes.append(f"`{_shown(node.raw)}` opens a section that never closes, so it stays as written.")
        siblings = stack[-1].children if stack else root
        at = next(index for index, item in enumerate(siblings) if item is node)
        siblings[at:at + 1] = [_Node("kept", raw=node.line), *(node.children or [])]
    return root


def _shown(text: str) -> str:
    """A tag for a note: on one line, and short."""
    line = " ".join(text.split())
    return line if len(line) <= 80 else line[:77] + "..."


# ---- reading a tag: the expression inside it ----

class _Expression:
    """A tag's expression as a tree of tuples. It reads what the other engine's expressions hold, and refuses the rest by name."""

    def __init__(self, source: str, message: bool = False) -> None:
        self.message = message
        self.quoted_twice = False  # whether a message's tag opens text with a double quote, which the other engine couldn't read there
        pattern = _TOKEN_IN_MESSAGE if message else _TOKEN
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(source):
            if source[position:].strip() == "":
                break
            match = pattern.match(source, position)
            if match is None:
                raise _NoMatch("isn't written in a way this reads")
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
            if len(self.tokens) > MAX_WORDS:
                raise _NoMatch("is too long to read")
        self.at = 0
        self.depth = 0

    def take(self) -> tuple[str, str]:
        if self.at >= len(self.tokens):
            raise _NoMatch("ends before it's finished")
        self.at += 1
        return self.tokens[self.at - 1]

    def sign(self, *signs: str) -> str | None:
        if self.at < len(self.tokens) and self.tokens[self.at][0] == "sign" and self.tokens[self.at][1] in signs:
            self.at += 1
            return self.tokens[self.at - 1][1]
        return None

    def whole(self) -> tuple:
        if not self.tokens:
            raise _NoMatch("is empty")
        tree = self.choice()
        if self.at < len(self.tokens):
            raise _NoMatch(f"has `{self.tokens[self.at][1]}` where nothing fits")
        return tree

    def choice(self) -> tuple:
        test = self.either()
        if self.sign("??"):
            raise _NoMatch("uses `??`, which has no match here")
        if not self.sign("?"):
            return test
        then = self.choice()
        return ("choice", test, then, self.choice() if self.sign(":") else None)

    def either(self) -> tuple:
        tree = self.both()
        while self.sign("||"):
            tree = ("or", tree, self.both())
        return tree

    def both(self) -> tuple:
        tree = self.comparison()
        while self.sign("&&"):
            tree = ("and", tree, self.comparison())
        return tree

    def comparison(self) -> tuple:
        tree = self.sum()
        while True:
            if self.at < len(self.tokens) and self.tokens[self.at] == ("name", "in"):
                raise _NoMatch("uses `in`, which has no match here")
            sign = self.sign("==", "!=", ">=", "<=", ">", "<", "=~", "!~")
            if sign is None:
                return tree
            if sign in ("=~", "!~"):
                raise _NoMatch(f"matches a pattern with `{sign}`, which a greeting has no block for")
            tree = ("compare", sign, tree, self.sum())

    def sum(self) -> tuple:
        tree = self.product()
        while True:
            sign = self.sign("+", "-", "&", "|", "^", "<<", ">>")
            if sign is None:
                return tree
            if sign not in "+-":
                raise _NoMatch(f"works on bits with `{sign}`, which the math block doesn't do")
            tree = ("math", sign, tree, self.product())

    def product(self) -> tuple:
        tree = self.power()
        while True:
            sign = self.sign("*", "/", "%")
            if sign is None:
                return tree
            tree = ("math", sign, tree, self.power())

    def power(self) -> tuple:
        tree = self.prefixed()
        while self.sign("**"):
            tree = ("math", "^", tree, self.prefixed())
        return tree

    def prefixed(self) -> tuple:
        self.depth += 1
        if self.depth > MAX_DEPTH * 2:
            raise _NoMatch("nests too deeply")
        try:
            sign = self.sign("!", "-", "~")
            if sign == "~":
                raise _NoMatch("works on bits with `~`, which the math block doesn't do")
            if sign is not None:
                return ("not" if sign == "!" else "minus", self.prefixed())
            return self.single()
        finally:
            self.depth -= 1

    def single(self) -> tuple:
        kind, value = self.take()
        if kind == "number":
            return ("number", value)
        if kind == "string":
            if not self.message:
                return ("string", _ESCAPED.sub(r"\1", value[1:-1]))
            self.quoted_twice = self.quoted_twice or value[0] == '"'
            return ("string", value[1:-1])
        if kind == "name":
            if value in ("true", "false"):
                return ("flag", value == "true")
            if not self.sign("("):
                return ("name", value)
            arguments = []
            if not self.sign(")"):
                while True:
                    arguments.append(self.choice())
                    if self.sign(")"):
                        break
                    if not self.sign(","):
                        raise _NoMatch(f"doesn't close the parentheses of `{value}`")
            return ("call", value, arguments)
        if value == "(":
            tree = self.choice()
            if self.sign(","):
                raise _NoMatch("holds a list in parentheses, which has no match here")
            if not self.sign(")"):
                raise _NoMatch("doesn't close its parentheses")
            return tree
        raise _NoMatch(f"has `{value}` where a value belongs")


# ---- writing the template language ----

class _Converter:
    def __init__(self, number_format: str) -> None:
        if number_format not in LOCALES:
            raise Invalid("The number format is one of " + ", ".join(LOCALES) + ".")
        self.number_format = LOCALES[number_format]
        self.notes: list[str] = []
        self.remarks: list[str] = []  # what to say about the tag being read, once it's known to convert
        self.message = False  # whether the text being read is a message's, which the other engine read as its stored JSON
        self.names = 0
        # One budget for the whole call, so a message of many parts costs no more than one part: the engine is asked about MAX_PROBES pairs
        # at most, within MAX_SECONDS of this thread's processor time, and every pair past that is written out without asking.
        self.probes = 0
        self.deadline = time.thread_time() + MAX_SECONDS

    def keep_clear_of(self, *texts: str) -> None:
        """Number this converter's variables past any `text` and a number the person's own text holds, so a brace of theirs never reads one.
        A number of more than 18 digits is past any this converter reaches, so it's passed over, and never read as a number."""
        for text in texts:
            for number in _OWN_NAME.findall(text):
                if len(number) <= 18:
                    self.names = max(self.names, int(number))

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def read(self, node: _Node, context: list[str]) -> _Value:
        """A tag's or a section's expression as a value. Whatever it remarks on becomes a note that names the tag."""
        self.remarks = []
        if self.message and _CONTROL.search(node.raw):
            raise _NoMatch("holds a line break or a tab, which the other bot read differently in a message")
        expression = _Expression(node.text, self.message)
        value = self.value(expression.whole(), context)
        if expression.quoted_twice:
            self.remarks.insert(0, _QUOTED_TWICE)
        for remark in self.remarks:
            self.note(f"`{_shown(node.raw)}` {remark}")
        return value

    # -- values --

    def value(self, tree: tuple, context: list[str]) -> _Value:
        kind = tree[0]
        if kind == "number":
            number = float(tree[1])
            text = str(int(number)) if number.is_integer() else tree[1]
            return _Value("number", _written(text), math=text, whole=number.is_integer())
        if kind == "string":
            if _DATED.match(tree[1]):
                raise _NoMatch(_dated(tree[1]))
            return _Value("text", _Parts().plain(tree[1]), literal=tree[1])
        if kind == "flag":
            word = "true" if tree[1] else "false"
            return _Value("flag", _written(word), test=_Test([(word, "==", "true")]))
        if kind == "name":
            return self.named(tree[1], context)
        if kind == "call":
            return self.call(tree[1], [self.value(argument, context) for argument in tree[2]])
        if kind == "not":
            return self.flag(self.test(self.value(tree[1], context), "`!`").negated())
        if kind == "minus":
            inner = self.value(tree[1], context)
            if inner.math is None:
                raise _NoMatch("puts a minus sign before something that isn't a number")
            return self.number(f"(-{inner.math})", inner.whole)
        if kind == "math":
            return self.math(tree[1], self.value(tree[2], context), self.value(tree[3], context))
        if kind == "compare":
            sign, sides = tree[1], tree[2:]
            dated = [side[0] == "string" and _DATED.match(side[1]) is not None for side in sides]
            if sign in ("==", "!=") and dated.count(True) == 1:
                # Text written as a date was a time or text there, and neither ever equals true or false, or an ID.
                other = self.value(sides[dated.index(False)], context)
                if _held(other) not in ("flag", "ID"):
                    raise _NoMatch(_dated(sides[dated.index(True)][1]))
                return self.flag(self.fixed(sign, *(_KINDS["dated"] if is_dated else _KINDS[_held(other)] for is_dated in dated)))
            return self.flag(self.compared(sign, self.value(sides[0], context), self.value(sides[1], context)))
        if kind in ("and", "or"):
            first = self.test(self.value(tree[1], context), "`&&`" if kind == "and" else "`||`")
            second = self.test(self.value(tree[2], context), "`&&`" if kind == "and" else "`||`")
            return self.flag(_joined("all" if kind == "and" else "any", first, second))
        if kind == "choice":
            test = self.test(self.value(tree[1], context), "`?`")
            chosen = self.value(tree[2], context)
            then = self.printed(chosen)
            if tree[3] is None:
                return _Value("text", self.choose(test, then), held="maybe")  # there it gave nothing at all when the test failed
            other = self.value(tree[3], context)
            held = _held(chosen) if _held(chosen) == _held(other) else "mixed"
            return _Value("text", self.choose(test, then, self.printed(other)), held=held)
        raise _NoMatch("isn't something this reads")

    def named(self, name: str, context: list[str]) -> _Value:
        """A name read inside sections on whole things. The other engine fills its names from the innermost section out, each overwriting the
        last, and the top-level names last of all, so a top-level name wins, then the outermost section that has the name."""
        for prefix in ["", *context]:
            found = _NAMES.get(f"{prefix}.{name}" if prefix else name)
            if found is not None:
                return found()
        raise _NoMatch("has no match in the template language")

    def printed(self, value: _Value) -> _Parts:
        if value.parts is None:
            raise _NoMatch("names a whole invite, which has no one value to show")
        return value.parts

    def number(self, math: str, whole: bool) -> _Value:
        """A number worked out by the math block. A whole one is cut to a whole number, since the block answers 4.0 for 2+2."""
        if not whole and _DECIMAL not in self.remarks:
            self.remarks.append(_DECIMAL)
        shown = "{m:trunc" + math + "}" if whole else "{m:" + math + "}"
        return _Value("number", _written(shown), math=math, whole=whole)

    def math(self, sign: str, first: _Value, second: _Value) -> _Value:
        kinds = (_held(first), _held(second))
        if sign == "+" and "text" in kinds:
            if "maybe" in kinds:
                raise _NoMatch("joins text to a choice with no `:` part, which the other bot wrote as `<nil>` when the choice gave nothing")
            return _Value("text", _Parts().add(self.printed(first)).add(self.printed(second)))
        if first.math is None or second.math is None:
            raise _NoMatch(f"does math with `{'**' if sign == '^' else sign}` on something that isn't a number")
        return self.number(f"({first.math}{sign}{second.math})", first.whole and second.whole and sign in "+-*%")

    def flag(self, test: _Test) -> _Value:
        if test.mode == "if" and not test.flipped and test.terms[0][1:] == ("==", "true"):
            return _Value("flag", _written(test.terms[0][0]), test=test)
        return _Value("flag", self.choose(test, _written("true"), _written("false")), test=test)

    def test(self, value: _Value, sign: str) -> _Test:
        if value.test is None:
            raise _NoMatch(f"uses {sign} on something that isn't true or false")
        return value.test

    def side(self, value: _Value, *, as_number: bool) -> str:
        """One side of a comparison. The person's own text can sit there only when none of it would read as the condition's punctuation."""
        if as_number:
            if value.math is None:
                raise _NoMatch("compares sizes of things that aren't numbers")
            return "{m:" + value.math + "}" if value.math.startswith("(") else value.math
        if value.kind in ("time", "thing"):
            raise _NoMatch("compares a time or a whole person, server or invite, which has no match here")
        parts = self.printed(value)
        for text, own in parts.items:
            if own and _BREAKS_A_CONDITION.search(text):
                raise _NoMatch("compares against text holding a pipe, a brace, a parenthesis or a comparison sign, which a condition can't hold")
        return "".join(text for text, _ in parts.items)

    def compared(self, sign: str, first: _Value, second: _Value) -> _Test:
        if sign in ("==", "!="):
            kinds = (_held(first), _held(second))
            if {"mixed", "maybe"} & set(kinds):
                raise _NoMatch(f"compares with `{sign}` a choice that can give two kinds of value, and the other bot counted values of two kinds as different")
            if kinds[0] != kinds[1] and not {"time", "thing"} & set(kinds):
                return self.fixed(sign, _KINDS[kinds[0]], _KINDS[kinds[1]])
        sizes = sign not in ("==", "!=")
        return _Test([(self.side(first, as_number=sizes), sign, self.side(second, as_number=sizes))])

    def fixed(self, sign: str, first: str, second: str) -> _Test:
        """A comparison of two kinds of value, which the other engine's == and != answered the same way every time."""
        answer = "false" if sign == "==" else "true"
        self.remarks.append(f"compares {first} with {second}, two kinds of value the other bot never counted equal, so its `{sign}` is always {answer}.")
        return _Test([(answer, "==", "true")])

    def truth(self, value: _Value) -> _Test | None:
        """Whether a section shows: a flag that's true, a number that isn't 0, text or a time that isn't empty. None means it always shows."""
        if value.kind == "thing":
            return None
        if value.test is not None:
            return value.test
        if value.kind == "time":
            return _Test([(value.date or "", "!=", "")])
        text = self.printed(value).flat()
        if text is None:
            raise _NoMatch("makes a section out of fixed text, which has no match here")
        return _Test([(text, "!=", {"number": "0", "flag": "false"}.get(_held(value), ""))])

    def call(self, name: str, arguments: list[_Value]) -> _Value:
        if name in ("Ordinal", "FormatNumber"):
            if not 1 <= len(arguments) <= 2:
                raise _NoMatch(f"gives `{name}` {len(arguments)} values, and it takes a number and sometimes a format")
            self.takes(name, arguments[0], "number")
            style = self.number_format
            if len(arguments) == 2:
                style = LOCALES.get((arguments[1].literal or "").strip().lower())
                if style is None:
                    raise _NoMatch(f"names a number format `{name}` doesn't have. The formats are " + ", ".join(LOCALES))
            block = "ord" if name == "Ordinal" else "num"
            head = "{" + block + ":" if style == "commas" else "{" + block + f"({style}):"
            return _Value("text", _written(head).add(self.printed(arguments[0])).written("}"))
        if name in ("Upper", "Lower", "Title"):
            if len(arguments) != 1:
                raise _NoMatch(f"gives `{name}` {len(arguments)} values, and it takes one")
            shown = self.printed(arguments[0])
            self.takes(name, arguments[0], "text")
            return _Value("text", _written("{" + name.lower() + ":").add(shown).written("}"))
        if name == "SinceTime":
            if len(arguments) != 1 or arguments[0].date is None:
                raise _NoMatch("gives `SinceTime` something that isn't one time")
            return _Value("text", _written("{since:" + arguments[0].date + "}"))
        if name == "FormatTime":
            if len(arguments) != 2 or arguments[0].date is None or arguments[1].literal is None:
                raise _NoMatch("gives `FormatTime` something other than a time and a format in quotes")
            return _Value("text", _written("{strf(" + arguments[0].date + "):").plain(date_codes(arguments[1].literal)).written("}"))
        raise _NoMatch(f"calls `{name}`, which has no match in the template language")

    @staticmethod
    def takes(name: str, value: _Value, kind: str) -> None:
        """Each of the other engine's functions took one kind of value and refused the rest, which stopped the whole message."""
        if _held(value) != kind:
            raise _NoMatch(f"gives `{name}` {_KINDS[_held(value)]}, and the other bot's `{name}` takes only {_KINDS[kind]}")

    # -- blocks --

    def choose(self, test: _Test, then: _Parts, otherwise: _Parts | None = None) -> _Parts:
        """An if, any or all block around the two texts. Text that holds a pipe goes through two variables, because the block would divide at it."""
        otherwise = otherwise or _Parts()
        if test.flipped:
            then, otherwise = otherwise, then
            test = _Test(test.terms, test.mode)
        out = _Parts()
        if not then.pipes and not otherwise.pipes:
            out.written("{" + test.head() + ":").add(then)
            if otherwise.items:
                out.written("|").add(otherwise)
            return out.written("}")
        self.names += 2
        first, second = self.names - 1, self.names
        out.written("{=(text%d):" % first).add(then).written("}{=(text%d):" % second).add(otherwise).written("}")
        # The read gives back the text with its pipe, so a block around this one goes through variables too. `add` has already marked that.
        return out.written("{text{" + test.head() + ":%d|%d}}" % (first, second))

    def body(self, nodes: list[_Node], context: list[str]) -> _Parts:
        out = _Parts()
        for node in nodes:
            if node.kind == "text":
                out.plain(html.unescape(node.text))
            elif node.kind == "kept":
                out.plain(node.raw)
            elif node.kind == "tag":
                try:
                    out.add(self.printed(self.read(node, context)))
                except _NoMatch as why:
                    self.note(f"`{_shown(node.raw)}` {why}, so it stays as written.")
                    out.plain(node.raw)
            else:
                out.add(self.section(node, context))
        return out

    def section(self, node: _Node, context: list[str]) -> _Parts:
        try:
            value = self.read(node, context)
            test = self.truth(value)
        except _NoMatch as why:
            self.note(f"`{_shown(node.raw)}` {why}, so the section's tags stay as written around its text.")
            return _Parts().plain(node.line).add(self.body(node.children or [], context)).plain(node.closing)
        if test is None:
            if node.inverted:
                self.note(f"`{_shown(node.raw)}` is a section that never shows, so its text was left out.")
                return _Parts()
            # Inside a section named for a whole person, server or invite, a bare name such as Name reads that thing's own.
            return self.body(node.children or [], [*context, value.prefix or ""])
        inner = self.body(node.children or [], context)
        if not inner.items:
            return _Parts()
        return self.choose(test.negated() if node.inverted else test, inner)

    def text(self, source: str, message: bool = False) -> str:
        if len(source) > MAX_TEXT:
            raise Invalid(f"This is {len(source)} characters, and one piece of text converts up to {MAX_TEXT}.")
        self.message = message
        made, braces = _assembled(self.body(_read(source, self.notes, not message), []).items, self)
        if braces:
            self.note(BRACE_NOTE)
        return made


def date_codes(pattern: str) -> str:
    """A time format written with date tags such as yyyy and MMMM, as the codes the strf block reads. Anything that isn't a tag stays as it is."""
    out = []
    position = 0
    while position < len(pattern):
        for tag, code in _DATE_TAGS:
            if pattern.startswith(tag, position):
                out.append(code)
                position += len(tag)
                break
        else:
            out.append("%%" if pattern[position] == "%" else pattern[position])
            position += 1
    return "".join(out)


def _probe_scope() -> Scope:
    """Everything a greeting can read, filled with stand-ins, to see whether a piece of plain text would run as a block."""
    role = SimpleNamespace(id=1, name="role", position=1, mention="<@&1>", permissions=[], color=SimpleNamespace(value=0))
    person = SimpleNamespace(id=1, name="name", display_name="name", mention="<@1>", bot=False, roles=[role], top_role=role, created_at=None, joined_at=None,
                             display_avatar=SimpleNamespace(url="https://example.com/avatar.png"))
    guild = SimpleNamespace(id=1, name="server", icon=None, member_count=1, members=[person], roles=[role], channels=[], owner=person, owner_id=1, created_at=None)
    channel = SimpleNamespace(id=1, name="channel", mention="<#1>", topic="", position=0, slowmode_delay=0)
    return Scope(guild=guild, member=person, channel=channel, target=person, args="one two three", uses=1, role=role, role_added=True,
                 extra={"joined": "1", "invite": objects.invite(None)})


def _stays_plain(snippet: str, written: str, deadline: float) -> bool:
    """Whether the engine gives `written` back as the plain text `snippet`. The engine's own time limit is cut to what the conversion call has left."""
    try:
        run = Interpreter(_probe_scope())
        run._deadline = min(run._deadline, deadline)
        return run.render(written) == snippet.strip("\n ")
    except LimitReached:
        return False
    except Exception:  # whatever this text does to the engine, it isn't plain text to it
        return False


def _assembled(items: list[tuple[str, bool]], budget: _Converter) -> tuple[str, bool]:
    """The pieces as one template, and whether any brace had to be written out. The person's own braces are written as blocks that give a brace wherever the engine would otherwise read them as a block's."""
    characters: list[str] = []
    own: list[bool] = []  # whether each character is the person's own text, outside every block the converter wrote
    depth = 0
    written_out = False
    for text, mine in items:
        if not mine:
            depth += text.count("{") - text.count("}")
            characters.extend(text)
            own.extend([False] * len(text))
        elif depth > 0:
            # Inside a block the converter wrote, any brace of theirs would open or close that block early.
            for character in text:
                piece = OPEN_BRACE if character == "{" else CLOSE_BRACE if character == "}" else character
                written_out = written_out or character in "{}"
                characters.extend(piece)
                own.extend([False] * len(piece))
        else:
            characters.extend(text)
            own.extend([True] * len(text))
    pairs: list[tuple[int, int]] = []
    opened: list[int] = []
    theirs = [0]  # how many of the characters before each place are the person's own
    for index, character in enumerate(characters):
        theirs.append(theirs[-1] + own[index])
        if character == "{":
            opened.append(index)
        elif character == "}" and opened:
            pairs.append((opened.pop(), index))
    swapped: dict[int, str] = {}
    # The braces are paired the way the engine pairs them. A pair that opens in the person's own text runs as a block only if the engine says so,
    # and one with the converter's blocks inside it can't be asked about ahead of time, so it's written out as braces.
    for start, end in sorted(pairs, key=lambda pair: pair[1] - pair[0]):
        if not own[start]:
            continue
        size = end + 1 - start
        if theirs[end + 1] - theirs[start] == size and size <= MAX_PROBED and budget.probes < MAX_PROBES and time.thread_time() < budget.deadline:
            budget.probes += 1
            snippet = "".join(characters[start:end + 1])
            if _stays_plain(snippet, "".join(swapped.get(index, characters[index]) for index in range(start, end + 1)), budget.deadline):
                continue
        swapped[start], swapped[end] = OPEN_BRACE, CLOSE_BRACE
    return "".join(swapped.get(index, character) for index, character in enumerate(characters)), written_out or bool(swapped)


# ---- what callers use ----

def convert_text(text: str, *, number_format: str = "plain", message: bool = False) -> tuple[str, list[str]]:
    """One piece of text with its double-brace tags written in the template language, and notes on anything that stayed as written.

    `number_format` is what a bare Ordinal(...) or FormatNumber(...) meant on the server the text comes from: plain, commas, dots, indian or arabic.
    `message` says the text is a message's own. The other bot rendered a message's stored JSON as it stood, with each line break written as a
    backslash and an n, so no section, closing or comment tag alone on its line took the line, and with a backslash before each double quote
    and backslash, which changes how text in quotes reads. Its picture text it rendered as plain text, where a tag alone on its line took the
    line. So picture text leaves `message` off, and a message's text passes True: convert_message does, and so does the Greetings page's
    Import for pasted text. Raises Invalid for text too long to convert.
    """
    if not isinstance(text, str):
        raise Invalid("The text to convert must be text.")
    converter = _Converter(number_format)
    converter.keep_clear_of(text)
    return converter.text(text, message), converter.notes


def _emoji_text(emoji: Any) -> str:
    """A button's emoji as a greeting stores it: the character itself, or a server emoji in Discord's own form."""
    if isinstance(emoji, str):
        return emoji
    if isinstance(emoji, dict):
        if emoji.get("id"):
            return f"<{'a' if emoji.get('animated') else ''}:{emoji.get('name') or 'emoji'}:{emoji['id']}>"
        return emoji.get("name") if isinstance(emoji.get("name"), str) else ""
    return ""


def _timestamp(value: Any) -> str | None:
    """An embed's timestamp as a greeting keeps it, or None when it isn't one."""
    try:
        return messages.clean_embed({"description": "-", "timestamp": value}).get("timestamp")
    except Invalid:
        return None


def _usable_link(value: str) -> bool:
    """What a stored greeting takes as a link: a web address, or a template that starts as one or with a block."""
    if value.startswith(("{", "http://", "https://")) and "{" in value and "}" in value and len(value) <= 2048:
        return True
    try:
        _URL(value)
    except Invalid:
        return False
    return True


def _texts_of(data: dict, listed: list[dict]) -> list[tuple[str, int, Any]]:
    """Each piece of text of a message that converts, as the sentence a stored greeting names it by, its limit and its value, in the order a stored
    greeting is checked. A part that isn't text is left to the conversion, which says so."""
    out: list[tuple[str, int, Any]] = [("The message text", messages.CONTENT_MAX, data.get("content"))]
    embeds = data.get("embeds") if isinstance(data.get("embeds"), list) else []
    for number, raw in enumerate(embeds[:messages.EMBEDS_MAX], 1):
        if not isinstance(raw, dict):
            continue
        footer = raw.get("footer") if isinstance(raw.get("footer"), dict) else {}
        author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
        out += [(f"Embed {number}: Title", messages.TITLE_MAX, raw.get("title")), (f"Embed {number}: Description", messages.DESCRIPTION_MAX, raw.get("description")),
                (f"Embed {number}: Footer", messages.FOOTER_MAX, footer.get("text")), (f"Embed {number}: Author name", messages.AUTHOR_MAX, author.get("name"))]
        fields = raw.get("fields") if isinstance(raw.get("fields"), list) else []
        for index, field in enumerate(fields[:messages.FIELDS_MAX], 1):
            entry = field if isinstance(field, dict) else {}
            out += [(f"Embed {number}: Field {index} name", messages.FIELD_NAME_MAX, entry.get("name")), (f"Embed {number}: Field {index} value", messages.FIELD_VALUE_MAX, entry.get("value"))]
    for number, part in enumerate(listed[:messages.BUTTONS_MAX], 1):
        out.append((f"Button {number} label", messages.BUTTON_LABEL_MAX, part.get("label")))
    return out


def _strings(value: Any) -> list[str]:
    """Every piece of text anywhere in pasted data."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _refuse_long_parts(data: dict, listed: list[dict]) -> None:
    """A part already longer than a message takes, as it was pasted, is refused before any work, in the words a stored greeting gets, so a paste
    built to be slow costs nothing."""
    total = 0
    for name, limit, value in _texts_of(data, listed):
        if name.startswith("Button ") and total > messages.TOTAL_MAX:
            break  # the embeds' total is checked before the buttons, as a stored greeting is
        if not isinstance(value, str):
            continue
        if len(value) > limit:
            raise Invalid(f"{name} can be at most {limit} characters. This is {len(value)}.")
        if name.startswith("Embed "):
            total += len(value)
    if total > messages.TOTAL_MAX:
        raise Invalid(f"All embeds together can hold at most {messages.TOTAL_MAX} characters. These hold {total}.")


def convert_message(data: Any, *, number_format: str = "plain") -> tuple[dict, list[str]]:
    """A Discord message object, as a dict or as JSON text, made into a greeting's stored message with its tags converted.

    It reads content, embeds (title, description, url, color, timestamp, fields, image, thumbnail, footer, author) and link buttons. Each note
    says which part it is about. Raises Invalid when this isn't a message, or when what it holds can't fit a message at all. A part already
    longer than a message takes is refused before any of it is converted, and the whole call shares one budget of the engine's time. A part
    that only conversion took past its limit comes back whole, with a note, as fitted_message says.
    """
    if isinstance(data, str):
        try:
            data = read_json_text(data)
        except ValueError as error:
            raise Invalid("That isn't JSON a message can be read from.") from error
    if isinstance(data, list):
        data = {"embeds": data}
    if not isinstance(data, dict):
        raise Invalid("That JSON isn't a message or an embed.")
    backups = data.get("messages")
    if isinstance(backups, list) and backups and isinstance(backups[0], dict):
        data = backups[0]["data"] if isinstance(backups[0].get("data"), dict) else backups[0]
    if isinstance(data.get("embed"), dict):
        data = {**data, "embeds": [data["embed"]]}
    if not any(key in data for key in ("content", "embeds", "buttons", "components")):
        data = {"embeds": [data]}
    listed = [part for part in data.get("buttons") or [] if isinstance(part, dict)] if isinstance(data.get("buttons"), list) else []
    other = 0
    for row in data.get("components") if isinstance(data.get("components"), list) else []:
        for part in (row.get("components") if isinstance(row, dict) and isinstance(row.get("components"), list) else []):
            if isinstance(part, dict) and part.get("url"):
                listed.append({"url": part["url"], "label": part.get("label"), "emoji": part.get("emoji")})
            else:
                other += 1
    _refuse_long_parts(data, listed)
    converter = _Converter(number_format)
    converter.keep_clear_of(*_strings(data))  # a variable the text sets is there for the embeds too, so every part counts
    notes: list[str] = []

    def say(where: str, text: str) -> None:
        line = f"{where}: {text}"
        if line not in notes:
            notes.append(line)

    def words(value: Any, where: str) -> str:
        """One part's text, converted. Something that isn't text is nothing."""
        if value is None or value == "":
            return ""
        if not isinstance(value, str):
            say(where, "this isn't text, so it was left out.")
            return ""
        before = len(converter.notes)
        made = converter.text(value, message=True)  # a message's text, as the other bot read it from the stored JSON
        for note in converter.notes[before:]:
            say(where, note)
        del converter.notes[before:]
        return made

    def link(value: Any, where: str) -> str:
        if isinstance(value, str) and len(value) > LINK_MAX:
            say(where, f"`{_shown(value)}` isn't a link, so it was left out.")  # no link is that long, and converting can't shorten it enough to matter
            return ""
        made = words(value, where).strip()
        if made and not _usable_link(made):
            say(where, f"`{_shown(made)}` isn't a link, so it was left out.")
            return ""
        return made

    out: dict[str, Any] = {"content": words(data.get("content"), "Message text"), "embeds": [], "buttons": []}
    embeds = data.get("embeds") if isinstance(data.get("embeds"), list) else []
    if len(embeds) > messages.EMBEDS_MAX:
        say("Embeds", f"a message holds {messages.EMBEDS_MAX}, so the last {len(embeds) - messages.EMBEDS_MAX} were left out.")
    for number, raw in enumerate(embeds[:messages.EMBEDS_MAX], 1):
        where = f"Embed {number}"
        if not isinstance(raw, dict):
            say(where, "this isn't an embed, so it was left out.")
            continue
        embed: dict[str, Any] = {}
        title = words(raw.get("title"), f"{where} title")
        if title:
            embed["title"] = title
        description = words(raw.get("description"), f"{where} description")
        if description:
            embed["description"] = description
        url = link(raw.get("url"), f"{where} title link")
        if url and title:
            embed["url"] = url
        elif url:
            say(f"{where} title link", "a title link needs a title, so it was left out.")
        if raw.get("color") not in (None, ""):
            try:
                embed["color"] = hex_color(raw["color"])
            except Invalid:
                say(f"{where} color", f"`{_shown(str(raw['color']))}` isn't a color, so it was left out.")
        if raw.get("timestamp"):
            stamp = _timestamp(raw["timestamp"])
            if stamp:
                embed["timestamp"] = stamp
            else:
                say(f"{where} timestamp", f"`{_shown(str(raw['timestamp']))}` isn't a date and time, so it was left out.")
        footer = raw.get("footer") if isinstance(raw.get("footer"), dict) else {}
        footer_text = words(footer.get("text"), f"{where} footer")
        footer_icon = link(footer.get("icon_url"), f"{where} footer icon")
        if footer_text:
            embed["footer"] = {"text": footer_text, **({"icon_url": footer_icon} if footer_icon else {})}
        elif footer_icon:
            say(f"{where} footer icon", "a footer icon needs footer text, so it was left out.")
        author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
        author_name = words(author.get("name"), f"{where} author")
        author_parts = {key: made for key, label in (("url", "author link"), ("icon_url", "author icon")) if (made := link(author.get(key), f"{where} {label}"))}
        if author_name:
            embed["author"] = {"name": author_name, **author_parts}
        elif author_parts:
            say(f"{where} author", "an author link or icon needs an author name, so it was left out.")
        for key, label in (("image", "image"), ("thumbnail", "thumbnail")):
            part = raw.get(key) if isinstance(raw.get(key), dict) else {}
            address = link(part.get("url"), f"{where} {label}")
            if address:
                embed[key] = {"url": address}
        fields = raw.get("fields") if isinstance(raw.get("fields"), list) else []
        if len(fields) > messages.FIELDS_MAX:
            say(f"{where} fields", f"an embed holds {messages.FIELDS_MAX}, so the last {len(fields) - messages.FIELDS_MAX} were left out.")
        kept = []
        for index, field in enumerate(fields[:messages.FIELDS_MAX], 1):
            entry = field if isinstance(field, dict) else {}
            name, value = words(entry.get("name"), f"{where} field {index} name"), words(entry.get("value"), f"{where} field {index} value")
            if name and value:
                kept.append({"name": name, "value": value, "inline": bool(entry.get("inline"))})
            else:
                say(f"{where} field {index}", "a field needs a name and a value, so it was left out.")
        if kept:
            embed["fields"] = kept
        if not messages.is_empty_embed(embed):
            out["embeds"].append(embed)
        elif raw:
            say(where, "it has nothing to show, so it was left out.")
    if other:
        say("Buttons", f"{other} of them {'is' if other == 1 else 'are'} not a link button. A greeting carries link buttons only, so {'it was' if other == 1 else 'they were'} left out.")
    if len(listed) > messages.BUTTONS_MAX:
        say("Buttons", f"a message holds {messages.BUTTONS_MAX}, so the last {len(listed) - messages.BUTTONS_MAX} were left out.")
    for number, part in enumerate(listed[:messages.BUTTONS_MAX], 1):
        where = f"Button {number}"
        address = link(part.get("url"), where)
        label = words(part.get("label"), f"{where} label")
        emoji = _emoji_text(part.get("emoji"))[:100]
        if address and (label or emoji):
            out["buttons"].append({"url": address, **({"label": label} if label else {}), **({"emoji": emoji} if emoji else {})})
        elif address or not part.get("url"):  # a link that was refused has its own note already
            say(where, "a button needs a link and a label or an emoji, so it was left out.")
    unread = sorted(key for key in data if key not in ("content", "embeds", "embed", "buttons", "components") and data[key] not in (None, "", [], {}, False))
    if unread:
        say("Message", "a greeting has no place for " + ", ".join(f"`{_shown(str(key))}`" for key in unread[:10]) + ", so that was left out.")
    return fitted_message(out, notes), notes


def _characters(number: int) -> str:
    return f"{number:,} character" + ("" if number == 1 else "s")


def _slots(payload: dict) -> list[tuple[str, dict, str, int]]:
    """Each piece of a message's text that has a limit of its own: where it is, what holds it, its key, and the limit."""
    slots = [("Message text", payload, "content", messages.CONTENT_MAX)]
    for number, embed in enumerate(payload.get("embeds") or [], 1):
        where = f"Embed {number}"
        slots += [(f"{where} title", embed, "title", messages.TITLE_MAX), (f"{where} description", embed, "description", messages.DESCRIPTION_MAX)]
        if isinstance(embed.get("footer"), dict):
            slots.append((f"{where} footer", embed["footer"], "text", messages.FOOTER_MAX))
        if isinstance(embed.get("author"), dict):
            slots.append((f"{where} author", embed["author"], "name", messages.AUTHOR_MAX))
        for index, field in enumerate(embed.get("fields") or [], 1):
            slots += [(f"{where} field {index} name", field, "name", messages.FIELD_NAME_MAX), (f"{where} field {index} value", field, "value", messages.FIELD_VALUE_MAX)]
    for number, button in enumerate(payload.get("buttons") or [], 1):
        slots.append((f"Button {number} label", button, "label", messages.BUTTON_LABEL_MAX))
    return [slot for slot in slots if isinstance(slot[1].get(slot[2]), str)]


def fitted_message(payload: dict, notes: list[str]) -> dict:
    """A converted message, checked as Save checks a greeting, with notes added to `notes`.

    A converted tag is longer than the one it replaces, so a part inside its limit as pasted can pass it once converted. That part comes back
    whole, with a note on how far over it is, so the person can shorten it in the editor. Save refuses the message until then. Everything
    else is checked as Save checks it and raises Invalid the same way. convert_message ends with this, and so does the Greetings page's
    Import for pasted text.
    """
    slots = _slots(payload)
    over = [f"{where}: once converted this is {_characters(len(holder[key]))}, and it can hold {limit:,}. Save refuses the message until it is "
            f"{_characters(len(holder[key]) - limit)} shorter." for where, holder, key, limit in slots if len(holder[key]) > limit]
    total = sum(messages.embed_size(embed) for embed in payload.get("embeds") or [])
    if total > messages.TOTAL_MAX:
        over.append(f"Embeds: once converted they hold {_characters(total)} together, and a message's embeds can hold {messages.TOTAL_MAX:,}. "
                    f"Save refuses the message until they are {_characters(total - messages.TOTAL_MAX)} shorter.")
    if not over:
        return messages.clean_payload(payload, templates=True)
    texts = [holder[key] for _, holder, key, _ in slots]
    for _, holder, key, _ in slots:
        holder[key] = "x"  # one character each, so the check below finds only what isn't a length
    checked = messages.clean_payload(payload, templates=True)
    for (_, holder, key, _), text in zip(_slots(checked), texts):
        holder[key] = text
    notes.extend(over)
    return checked


__all__ = ["LOCALES", "convert_message", "convert_text", "date_codes", "fitted_message"]
