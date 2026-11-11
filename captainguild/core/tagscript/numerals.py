"""Whole numbers written out for the ord and num blocks: digits in groups, and the ending of an ordinal.

A format decides what sits between the groups and which numerals draw the number. With no format, both blocks group with commas.
"""
from __future__ import annotations

FORMATS = ("plain", "commas", "dots", "indian", "arabic")
DEFAULT = "commas"

_ARABIC_DIGITS = {ord("0") + digit: 0x0660 + digit for digit in range(10)}  # the Arabic-Indic digits, U+0660 to U+0669
_ARABIC_SEPARATOR = chr(0x066C)  # the Arabic thousands separator


def _lakh(digits: str) -> str:
    """The last three digits, then pairs, as India writes large numbers: 12,34,567."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.append(head[-2:])
        head = head[:-2]
    pairs.append(head)
    return ",".join(reversed(pairs)) + "," + tail


def grouped(number: int, style: str = DEFAULT) -> str:
    """8102 as 8102, 8,102 or 8.102, by the format. A format nobody has heard of groups with commas."""
    sign = "-" if number < 0 else ""
    size = abs(number)
    if style == "plain":
        return sign + str(size)
    if style == "dots":
        return sign + f"{size:,}".replace(",", ".")
    if style == "indian":
        return sign + _lakh(str(size))
    if style == "arabic":
        return sign + f"{size:,}".replace(",", _ARABIC_SEPARATOR).translate(_ARABIC_DIGITS)
    return sign + f"{size:,}"


def ending(number: int) -> str:
    """st, nd, rd or th."""
    if 10 <= abs(number) % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(abs(number) % 10, "th")


def ordinal(number: int, style: str = DEFAULT) -> str:
    """8102 as 8,102nd, with the digits written by the format."""
    return grouped(number, style) + ending(number)
