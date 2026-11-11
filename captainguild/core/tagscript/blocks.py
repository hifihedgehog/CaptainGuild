"""Every block the language has, as one table from name to function.

A block function takes the run, the lower-case name it was called by, its parameter and its payload, both None when absent. It returns the text
that replaces the block, or None when the block can't work with what it was given. None leaves the name to be tried as a variable and, failing
that, leaves the block as written.
"""
from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING, Callable
from urllib.parse import quote, quote_plus

from ..durations import ordinal
from . import numerals
from .clock import describe_gap, format_time, from_unix, parse_when
from .mathexpr import evaluate
from .text import (KEEPING, MAX_COMMANDS, MAX_REACTIONS, MAX_VALUE, SHIELDING, LimitReached, carry, is_shielded, map_runs, plain, replace_keeping,
                   split_list, title_case)

if TYPE_CHECKING:
    from .engine import Interpreter

MAX_FORMAT = 2000  # characters in a date format. A code can write far more than it takes up, and nobody needs a page of them.

_COMPARISONS = ("!=", "==", ">=", "<=", ">", "<")
_WEIGHT = re.compile(r"\s*(\d{1,9})\|")
_RANGE = re.compile(r"\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*")
_NUMBER_WORD = re.compile(r"-?\d+(?:\.0+)?")


def compare(expression: str) -> bool | None:
    """One boolean equation. Only operators in the template count, since text from outside is shielded. None when the sides aren't numbers."""
    for operator in _COMPARISONS:
        if operator in expression:
            left, right = expression.split(operator)[:2]
            left, right = plain(left).strip(), plain(right).strip()
            if operator == "!=":
                return left != right
            if operator == "==":
                return left == right
            try:
                first, second = float(left), float(right)
            except ValueError:
                return None
            if operator == ">=":
                return first >= second
            if operator == "<=":
                return first <= second
            return first > second if operator == ">" else first < second
    return None


def _branch(payload: str, result: bool) -> str:
    """Then and else split on one pipe. With any other number of pipes the whole payload is the then part."""
    parts = payload.split("|")
    if len(parts) == 2:
        return parts[0] if result else parts[1]
    return payload if result else ""


def _if(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    result = compare(parameter)
    return None if result is None else _branch(payload, result)


def _any(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    return _branch(payload, any(compare(part) for part in parameter.split("|")))


def _all(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    return _branch(payload, all(compare(part) for part in parameter.split("|")))


def _break(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is not None and compare(parameter):
        run.body = payload if payload is not None else ""
    return ""


def _stop(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None:
        return None
    if compare(parameter):
        run.stopped = True
        return payload if payload is not None else ""
    return ""


def _assign(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None:
        return None
    run.variables[plain(parameter)] = payload if payload is not None else ""
    return ""


def _math(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    return evaluate(plain(payload))


def _generator(seed: str | None) -> random.Random | None:
    return random.Random(plain(seed)) if seed is not None else None


def _random(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    rows, total = [], 0
    for item in split_list(payload):
        match = _WEIGHT.match(plain(item))
        weight = int(match.group(1)) if match else 1
        if weight:
            rows.append((weight, item[match.end():] if match else item))
            total += weight
    if not rows:
        return ""
    generator = _generator(parameter)
    # A seeded pick lands where a choice from the list written out in full would, so a weight is the same as repeating the element.
    roll = generator.randrange(total) if generator else random.randrange(total)
    for weight, text in rows:
        roll -= weight
        if roll < 0:
            return text
    return rows[-1][1]


def _range(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    match = _RANGE.fullmatch(plain(payload))
    if match is None:
        return None
    scale = 10 if name == "rangef" else 1
    try:
        if scale == 10:
            low, high = round(float(match.group(1)) * scale), round(float(match.group(2)) * scale)
        else:
            low, high = int(float(match.group(1))), int(float(match.group(2)))
    except (OverflowError, ValueError):
        return None
    low, high = min(low, high), max(low, high)
    generator = _generator(parameter)
    picked = generator.randint(low, high) if generator else random.randint(low, high)
    return str(picked / 10) if scale == 10 else str(picked)


def _fifty(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    return payload if random.random() < 0.5 else ""


def _strf(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None or len(payload) > MAX_FORMAT:
        return None
    moment = from_unix(run.now)
    if parameter is not None and plain(parameter).strip():
        moment = parse_when(plain(parameter))
        if moment is None:
            return None
    if is_shielded(parameter):
        # The date came from outside. The result stays data as a whole and reads as it always did, the codes found in the format's plain text.
        # What each code gives is from outside, and the rest of the format keeps its own marks: the template's text still counts as the
        # template's, so a mention typed in it is the template's mention, a strf's inside it included, and text from outside stays outside.
        return format_time(moment, plain(payload), codes=SHIELDING, literal=payload.translate(KEEPING))
    return carry(format_time(moment, plain(payload)), parameter, payload)


def _timedelta(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    other = parse_when(plain(payload))
    if other is None:
        return None
    moment = from_unix(run.now)
    if parameter is not None and plain(parameter).strip():
        moment = parse_when(plain(parameter))
        if moment is None:
            return None
    # since names one unit, for words that go where a Discord timestamp can't be drawn, such as a picture.
    return describe_gap(moment, other, 1 if name == "since" else 3)


def _list(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    items = split_list(payload)
    try:
        index = int(plain(parameter))
    except ValueError:
        return "The list index must be a number."
    return items[index] if -len(items) <= index < len(items) else ""


def _cycle(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    items = split_list(payload)
    try:
        return items[int(plain(parameter)) % len(items)]
    except ValueError:
        return "The cycle index must be a number."


def _index(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    words = plain(payload).split()
    wanted = plain(parameter)
    return str(words.index(wanted)) if wanted in words else "-1"


def _in(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    return "true" if plain(parameter) in plain(payload) else "false"


def _contains(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    return "true" if plain(parameter) in plain(payload).split() else "false"


def _whole_number(payload: str) -> int | None:
    """The number ord and num read: digits, with a sign and the .0 the math block leaves. None for anything else."""
    text = plain(payload).strip()
    if len(text) > 40 or not _NUMBER_WORD.fullmatch(text):
        return None
    return int(text.split(".")[0])


def _number_format(parameter: str | None) -> str | None:
    """The format a parameter names, or None when it names none."""
    if parameter is None:
        return None
    style = plain(parameter).strip().lower()
    return style if style in numerals.FORMATS else None


def _ord(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    number = _whole_number(payload)
    if number is None:
        return payload
    style = _number_format(parameter)
    if style is None:
        return carry(ordinal(number), payload)
    return carry(numerals.ordinal(number, style), parameter, payload)


def _num(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    # A tag written before this block existed may hold a variable called num, so anything that isn't a number in a known format is left for the variable.
    style = _number_format(parameter)
    if payload is None or (parameter is not None and style is None):
        return None
    number = _whole_number(payload)
    if number is None:
        return None
    return carry(numerals.grouped(number, style or numerals.DEFAULT), parameter, payload)


def _lower(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    return None if payload is None else map_runs(payload, str.lower)


def _upper(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    return None if payload is None else map_runs(payload, str.upper)


def _title(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    # With a parameter this is a variable called title read a piece at a time, which older tags may do.
    return None if payload is None or parameter is not None else title_case(payload)


def _replaced(text: str, before: str, after: str) -> str:
    """The replace and join blocks, refusing up front a result that would be too large to build."""
    reading = plain(text)
    count = len(reading) + 1 if not before else reading.count(before)
    if len(reading) + count * (len(after) - len(before)) > MAX_VALUE:
        raise LimitReached("a replacement grew too large")
    return replace_keeping(text, before, after)


def _join(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    return _replaced(payload, " ", parameter)


def _replace(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    # The comma the author wrote is the divider, so a comma inside a value that was filled in doesn't move it.
    position = parameter.find(",")
    if position < 0:
        position = plain(parameter).find(",")
    if position < 0:
        return None
    return _replaced(payload, plain(parameter[:position]), parameter[position + 1:])


def _urlencode(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    plus = parameter is not None and plain(parameter) == "+"
    return map_runs(payload, (lambda text: quote_plus(text, safe="")) if plus else (lambda text: quote(text, safe="")))


def _substring(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None or payload is None:
        return None
    text = plain(parameter)
    try:
        if "-" in text:
            start, end = text.split("-")[:2]
            return payload[int(float(start)):int(float(end))]
        return payload[int(float(text)):]
    except (ValueError, OverflowError):
        return None


# ---- blocks that ask for an action. The text they leave behind is empty, and what they asked for waits in run.actions. ----

def _delete(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    run.actions.delete = True
    return ""


def _silent(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    run.actions.silent = True
    return ""


def _override(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    run.actions.override = True
    return ""


def _dm(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    run.actions.destination = "dm"
    run.actions.channel_text = None
    return ""


def _redirect(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    run.actions.destination = "channel"
    run.actions.channel_text = plain(payload).strip()
    return ""


def _limit(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    entries = [part.strip() for part in plain(payload).split(",") if part.strip()]
    message = plain(parameter) if parameter is not None else None
    (run.actions.requires if name == "require" else run.actions.blacklists).append((message, entries))
    return ""


def _react(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    wanted = run.actions.react if name == "react" else run.actions.react_up
    wanted.extend(plain(payload).split())
    del wanted[MAX_REACTIONS:]
    return ""


def _embed(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if parameter is None:
        return None
    key = plain(parameter).strip().lower()
    if key not in ("title", "url", "description", "color", "timestamp"):
        return None
    run.actions.embed.append((key, plain(payload) if payload is not None else ""))
    return ""


def _command(run: "Interpreter", name: str, parameter: str | None, payload: str | None) -> str | None:
    if payload is None:
        return None
    text = plain(payload).strip()
    if text and len(run.actions.commands) < MAX_COMMANDS:
        run.actions.commands.append((text, name == "cmdadmin"))
    return ""


Block = Callable[["Interpreter", str, "str | None", "str | None"], "str | None"]

BLOCKS: dict[str, Block] = {
    "=": _assign, "assign": _assign, "let": _assign, "var": _assign,
    "if": _if,
    "any": _any, "or": _any,
    "all": _all, "and": _all,
    "break": _break, "short": _break, "shortcircuit": _break,
    "stop": _stop, "halt": _stop,
    "math": _math, "m": _math, "+": _math, "calc": _math,
    "random": _random, "rand": _random, "#": _random,
    "range": _range, "rangef": _range,
    "5050": _fifty, "50": _fifty, "?": _fifty,
    "strf": _strf,
    "td": _timedelta, "since": _timedelta,
    "list": _list,
    "cycle": _cycle,
    "index": _index,
    "in": _in,
    "contains": _contains,
    "ord": _ord,
    "num": _num,
    "lower": _lower,
    "upper": _upper,
    "title": _title,
    "join": _join,
    "replace": _replace,
    "urlencode": _urlencode,
    "substr": _substring, "substring": _substring,
    "delete": _delete, "del": _delete,
    "silence": _silent, "silent": _silent,
    "override": _override,
    "dm": _dm,
    "redirect": _redirect,
    "require": _limit, "blacklist": _limit,
    "react": _react, "reactu": _react,
    "embed": _embed,
    "command": _command, "cmd": _command, "c": _command, "cmdadmin": _command,
}
# Blocks newer than templates that may already hold a variable of the same name, set by the template or given by the feature. Where there is
# such a variable, it wins in every form, as it did before the block existed.
GIVES_WAY = frozenset({"title", "num", "since"})
