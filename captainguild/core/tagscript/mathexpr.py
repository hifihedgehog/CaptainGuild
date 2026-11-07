"""Arithmetic for the math block, from a small parser of our own.

Numbers are floats, so 2+2 gives 4.0, and only trunc, round and sgn give whole numbers. Every cost is capped: the expression's length, how deep it
nests and how large a whole number can grow. A whole-number power is the one operation that could run for minutes on a short expression.
"""
from __future__ import annotations

import math
import re
from typing import Callable

MAX_LENGTH = 5000
MAX_DEPTH = 100
MAX_INT_BITS = 4096

_TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)|([A-Za-z][A-Za-z0-9_$]*)|(\+=|-=|\*=|/=|[-+*/%^()]))")
_EPSILON = 1e-12

Number = float | int


class MathFailed(Exception):
    pass


def _sign(value: Number) -> int:
    if abs(value) <= _EPSILON:
        return 0
    return 1 if value > 0 else -1


_FUNCTIONS: dict[str, Callable[[Number], Number]] = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "exp": math.exp, "abs": abs, "trunc": int, "round": round, "sgn": _sign,
    "log": math.log10, "ln": math.log, "log2": math.log2,
}


def _checked(value: Number) -> Number:
    if isinstance(value, complex):  # a negative number to a fractional power
        raise MathFailed
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise MathFailed
    return value


def _times(left: Number, right: Number) -> Number:
    if isinstance(left, int) and isinstance(right, int) and left.bit_length() + right.bit_length() > MAX_INT_BITS:
        raise MathFailed
    return left * right


def _power(base: Number, exponent: Number) -> Number:
    if isinstance(base, int) and isinstance(exponent, int) and exponent > 0 and base.bit_length() * exponent > MAX_INT_BITS:
        raise MathFailed
    return base ** exponent


_ADD_LEVEL = {"+": lambda a, b: a + b, "-": lambda a, b: a - b}
_MULTIPLY_LEVEL = {"*": _times, "/": lambda a, b: a / b, "%": lambda a, b: a % b}
_ASSIGN_LEVEL = {"+=": _ADD_LEVEL["+"], "-=": _ADD_LEVEL["-"], "*=": _times, "/=": _MULTIPLY_LEVEL["/"]}


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.index = 0
        self.depth = -1  # the top of an expression isn't nested in anything

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> str | None:
        token = self.peek()
        self.index += 1
        return token

    def whole(self) -> Number:
        value = self.sum()
        while self.peek() in _ASSIGN_LEVEL:
            operation = _ASSIGN_LEVEL[self.take()]
            value = _checked(operation(value, self.sum()))
        if self.peek() is not None:
            raise MathFailed
        return value

    def sum(self) -> Number:
        value = self.product()
        while self.peek() in _ADD_LEVEL:
            operation = _ADD_LEVEL[self.take()]
            value = _checked(operation(value, self.product()))
        return value

    def product(self) -> Number:
        value = self.power()
        while self.peek() in _MULTIPLY_LEVEL:
            operation = _MULTIPLY_LEVEL[self.take()]
            value = _checked(operation(value, self.power()))
        return value

    def power(self) -> Number:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise MathFailed
        base = self.atom()
        if self.peek() == "^":
            self.take()
            base = _checked(_power(base, self.power()))
        self.depth -= 1
        return base

    def atom(self) -> Number:
        negative = False
        while self.peek() in ("+", "-"):
            negative ^= self.take() == "-"
        value = self.unsigned()
        return -value if negative else value

    def unsigned(self) -> Number:
        token = self.take()
        if token is None:
            raise MathFailed
        if token == "(":  # `power` counts each level on the way through
            value = self.sum()
            if self.take() != ")":
                raise MathFailed
            return value
        if token[0].isdigit() or token[0] == ".":
            return float(token)
        if not (token[0].isalpha()):
            raise MathFailed
        name = token.lower()
        if self.peek() == "(":
            function = _FUNCTIONS.get(name)
            if function is None:
                raise MathFailed
            self.take()
            argument = self.sum()
            if self.take() != ")":
                raise MathFailed
            return _checked(function(argument))
        if name == "pi":
            return math.pi
        if name == "e":
            return math.e
        raise MathFailed


def _tokens(text: str) -> list[str]:
    tokens, position = [], 0
    stripped = text.rstrip()
    while position < len(stripped):
        match = _TOKEN.match(stripped, position)
        if match is None:
            raise MathFailed
        tokens.append(match.group(1) or match.group(2) or match.group(3))
        position = match.end()
    return tokens


def evaluate(text: str) -> str | None:
    """The result as text, or None when the expression can't be worked out."""
    if len(text) > MAX_LENGTH:
        return None
    try:
        return str(_Parser(_tokens(text)).whole())
    except (MathFailed, ArithmeticError, ValueError, RecursionError):
        return None
