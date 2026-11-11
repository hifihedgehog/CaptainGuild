"""Text on a welcome picture: the three marks, emoji, fonts for every script, wrapping, and shrinking to fit a box.

Text arrives filled in by the template language, with a note of which characters came from outside the template: a member's name, a server's
name. Only what the server wrote can be a mark or a server emoji. A member called **Sam** is drawn with the stars.

    **bold**   *italic*   __underline__   <:name:id>   and \\* for a star that stays a star

Each line is cut into pieces that share a font and a style. A piece is measured and drawn by the first font in the chain that has its
characters, so a name in Korean or Arabic draws beside a greeting in Latin letters. Standard emoji are drawn in color when the machine has a
color emoji font, the way the statistics cards draw them.
"""
from __future__ import annotations

import functools
import itertools
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, ImageChops, ImageDraw

from ..stats import draw as cards
from . import fonts

MAX_LINES = 40
MAX_DRAWN = 1500          # characters drawn from one text layer once the template is filled in
MAX_MARKS = 30            # marks kept on one letter, the most Unicode's stream-safe text allows in a row. A font stacks them upward.
_READ = 4 * MAX_DRAWN     # characters read before the rest is left out, room for the marks past MAX_MARKS that are dropped
SMALLEST = 8              # shrinking stops here, and what still doesn't fit is cut with an ellipsis
SMALLEST_SHARE = 0.2      # or at this share of the size asked for, whichever is larger
ITALIC_SLANT = 0.2        # how far a slanted letter leans, as run over rise
EMOJI_HEIGHT = cards.EMOJI_HEIGHT
EMOJI_SPACE = cards.EMOJI_SPACE
ELLIPSIS = chr(0x2026)

_CUSTOM = re.compile(r"<(a?):([A-Za-z0-9_]{1,32}):([0-9]{15,20})>")
# Marks that turn text around or set its direction. A name can hold them, and a picture's text must not be reversed by one.
_BIDI_CONTROLS = frozenset(map(chr, (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F, 0x061C)))
_LINE_BREAKS = frozenset(map(chr, (0x2028, 0x2029)))  # Unicode's own line and paragraph separators
_BLANKS = frozenset("\t\x0b\x0c")
_BYTE_ORDER_MARK = chr(0xFEFF)
_JOINER = chr(0x200D)
_EMOJI_PARTS = frozenset(map(chr, (0xFE0F, 0x200D, 0x20E3)))  # the emoji selector, the joiner between emoji, and the keycap
# Characters that are emoji unless a text selector follows, below the planes that hold nothing else.
_EMOJI_BY_DEFAULT = frozenset(map(chr, (
    0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23F0, 0x23F3, 0x25FD, 0x25FE, 0x2614, 0x2615, *range(0x2648, 0x2654), 0x267F, 0x2693, 0x26A1, 0x26AA, 0x26AB,
    0x26BD, 0x26BE, 0x26C4, 0x26C5, 0x26CE, 0x26D4, 0x26EA, 0x26F2, 0x26F3, 0x26F5, 0x26FA, 0x26FD, 0x2705, 0x270A, 0x270B, 0x2728, 0x274C, 0x274E, 0x2753, 0x2754, 0x2755,
    0x2757, 0x2795, 0x2796, 0x2797, 0x27B0, 0x27BF, 0x2B1B, 0x2B1C, 0x2B50, 0x2B55,
)))


@dataclass(frozen=True)
class Marked:
    """Text ready to draw. `outside` holds one byte for each character, 1 where it came from outside the template. With `marks` off, nothing in it is read as a mark."""
    text: str
    outside: bytes = b""
    marks: bool = True

    @staticmethod
    def of(text: str, *, marks: bool = True) -> "Marked":
        """Text the server wrote itself, every character of it."""
        return Marked(text, bytes(len(text)), marks)


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    emoji: str = ""  # a server emoji's ID, when the span is one
    outside: bytes = b""  # the outside notes of the text's characters, as in Marked


def _tidy(marked: Marked) -> Marked:
    """Line ends made plain, tabs made spaces, the characters that draw nothing or turn text around taken out, and letters typed as a base and
    its marks put in their composed form. The outside notes stay in step."""
    text, outside = marked.text, marked.outside if len(marked.outside) == len(marked.text) else bytes(len(marked.text))
    letters: list[str] = []
    flags = bytearray()
    for index, character in enumerate(text):
        if character == "\r":
            if text[index + 1:index + 2] == "\n":
                continue
            character = "\n"
        elif character in _LINE_BREAKS:
            character = "\n"
        elif character in _BLANKS:
            character = " "
        elif character in _BIDI_CONTROLS or character == _BYTE_ORDER_MARK or (character != "\n" and unicodedata.category(character) in ("Cc", "Cs")):
            continue
        letters.append(character)
        flags.append(outside[index])
        if len(letters) >= _READ:
            break
    # A joiner that ends a line or the text joins nothing, and the line break after it would join its cluster.
    ending = True
    for index in range(len(letters) - 1, -1, -1):
        if letters[index] == _JOINER and ending:
            del letters[index], flags[index]
            continue
        ending = letters[index] == "\n"
    # Each letter is put together with the marks typed after it, one letter at a time, and a character with no marks is kept as its owner
    # chose it, such as a kanji in its traditional form. A mark noted as outside never joins a letter the server wrote, so a name that starts
    # with a mark can't change the server's text before it, such as the > that closes a server emoji. When the notes cover only the plain
    # letters of outside text, a mark's source can't be told, so a letter the server wrote in plain ASCII is also left as written.
    kept: list[str] = []
    kept_flags = bytearray()
    marks = start = 0
    while start < len(letters):
        end = start + 1
        while end < len(letters) and unicodedata.category(letters[end]).startswith("M") and not (flags[end] and not flags[start]):
            end += 1
        written = "".join(letters[start:end])
        server_letter = letters[start].isascii() and not flags[start]
        has_marks = end > start + 1 or unicodedata.category(letters[start]).startswith("M")
        composed = unicodedata.normalize("NFC", written) if has_marks and not server_letter else written
        notes = flags[start:end] if composed == written else bytes([flags[start]]) * len(composed)
        for character, note in zip(composed, notes):
            marks = marks + 1 if unicodedata.category(character).startswith("M") else 0
            if marks > MAX_MARKS or len(kept) >= MAX_DRAWN:
                continue
            kept.append(character)
            kept_flags.append(note)
        start = end
    return Marked("".join(kept), bytes(kept_flags), marked.marks)


def _tokens(marked: Marked) -> list[tuple[str, str, bytes]]:
    """A tidied text with marks on as ("text", letters, notes), ("mark", "bold" and so on) with the ("marked", stars) that wrote it, and
    ("emoji", ID) for each server emoji the server wrote. A text token carries the outside notes of its letters, and the others carry none.
    One pass, so a text's server emoji are known without pairing its marks."""
    text, outside = marked.text, marked.outside
    tokens: list[tuple[str, str, bytes]] = []
    buffer: list[str] = []
    notes = bytearray()

    def flush() -> None:
        if buffer:
            tokens.append(("text", "".join(buffer), bytes(notes)))
            buffer.clear()
            notes.clear()

    def written(start: int, end: int) -> bool:
        return not any(outside[start:end])

    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if outside[index]:
            buffer.append(character)
            notes.append(outside[index])
            index += 1
            continue
        if character == "\\" and index + 1 < length and text[index + 1] in "*_<\\" and not outside[index + 1]:
            buffer.append(text[index + 1])
            notes.append(0)
            index += 2
            continue
        if character == "<":
            match = _CUSTOM.match(text, index)
            if match and written(match.start(), match.end()):
                flush()
                tokens.append(("emoji", match.group(3), b""))
                index = match.end()
                continue
        if character in "*_":
            run = 1
            while index + run < length and text[index + run] == character and not outside[index + run] and run < 3:
                run += 1
            if character == "*":
                names = {1: ["italic"], 2: ["bold"], 3: ["bold", "italic"]}[run]
            elif run >= 2:
                names, run = ["underline"], 2
            else:
                names = []
            if names:
                flush()
                for name in names:
                    tokens.append(("mark", name, b""))
                tokens.append(("marked", character * run, b""))  # what was written, in case the mark has no partner
                index += run
                continue
        buffer.append(character)
        notes.append(0)
        index += 1
    flush()
    return tokens


def parse(marked: Marked) -> list[Span]:
    """The text as spans that share a style. A mark counts only where the server wrote it, opens only before a letter and closes only after one, and one left open is drawn as written."""
    marked = _tidy(marked)
    if not marked.marks:
        return [Span(marked.text, outside=marked.outside)] if marked.text else []
    tokens = _tokens(marked)

    # A mark opens before a letter and closes after one. Whatever can't do either is text, and so is an opening with no close.
    def letters_before(at: int) -> str:
        for kind, value, _ in reversed(tokens[:at]):
            if kind in ("text", "emoji"):
                return value[-1:] if kind == "text" else "x"
        return ""

    def letters_after(at: int) -> str:
        for kind, value, _ in tokens[at + 1:]:
            if kind in ("text", "emoji"):
                return value[:1] if kind == "text" else "x"
        return ""

    opened: dict[str, int] = {}
    literal: set[int] = set()
    group_of: dict[int, int] = {}
    group_start = 0
    for at, (kind, value, _) in enumerate(tokens):
        if kind == "mark":
            if at == 0 or tokens[at - 1][0] != "mark":
                group_start = at
            group_of[at] = group_start
            continue
        if kind != "marked":
            continue
        first = group_start
        for mark_at in range(first, at):
            name = tokens[mark_at][1]
            if name in opened:
                before = letters_before(first)
                if before and not before.isspace():
                    del opened[name]
                else:
                    literal.add(mark_at)
            else:
                after = letters_after(at)
                if after and not after.isspace():
                    opened[name] = mark_at
                else:
                    literal.add(mark_at)
    literal.update(opened.values())

    spans: list[Span] = []
    state = {"bold": False, "italic": False, "underline": False}

    def add(letters: str, emoji: str = "", notes: bytes = b"") -> None:
        if not letters and not emoji:
            return
        notes = notes if len(notes) == len(letters) else bytes(len(letters))  # stars left as written are the server's
        last = spans[-1] if spans else None
        if last is not None and not emoji and not last.emoji and (last.bold, last.italic, last.underline) == (state["bold"], state["italic"], state["underline"]):
            spans[-1] = Span(last.text + letters, last.bold, last.italic, last.underline, outside=last.outside + notes)
        else:
            spans.append(Span(letters, state["bold"], state["italic"], state["underline"], emoji, notes))

    at = 0
    while at < len(tokens):
        kind, value, notes = tokens[at]
        if kind == "text":
            add(value, notes=notes)
        elif kind == "emoji":
            add("", value)
        elif kind == "mark":
            end = at
            while tokens[end][0] == "mark":
                end += 1
            marks = list(range(at, end))
            if all(mark in literal for mark in marks):
                add(tokens[end][1])
            else:
                written_as = tokens[end][1]
                if any(mark in literal for mark in marks):  # of three stars, some made a style and the rest are left over
                    add(written_as[0] * sum(1 if tokens[mark][1] == "italic" else 2 for mark in marks if mark in literal))
                for mark in marks:
                    if mark not in literal:
                        state[tokens[mark][1]] = not state[tokens[mark][1]]
            at = end
        at += 1
    return spans


def server_emoji(marked: Marked) -> list[str]:
    """The IDs of the server emoji a text draws, in the order they appear: the ones `parse` gives, in one pass over the text."""
    if "<" not in marked.text:
        return []  # every server emoji starts with one, and tidying the text never makes one
    marked = _tidy(marked)
    if not marked.marks:
        return []
    return [value for kind, value, _ in _tokens(marked) if kind == "emoji"]


# ---- pieces ----

@dataclass
class Piece:
    kind: str            # "text", "space", "emoji" for a standard emoji in color, "custom" for a server emoji
    text: str
    font: int = 0        # which font of the chain draws it
    bold: bool = False
    italic: bool = False
    underline: bool = False
    rtl: bool = False
    breaks: bool = False  # a line can end before this piece


def _clusters(text: str) -> list[str]:
    """Characters as a reader counts them: an emoji with its parts, a letter with its accents. A line break is always one of its own, even
    after a joiner, which takes whatever follows it into its emoji. The shaper refuses a line break inside the text it lays out."""
    out: list[str] = []
    for number, line in enumerate(text.split("\n")):
        if number:
            out.append("\n")
        start = len(out)
        for cluster in cards.clusters(line):
            code = ord(cluster[0])
            if len(out) > start and len(cluster) == 1 and (unicodedata.category(cluster).startswith("M") or 0xFE00 <= code <= 0xFE0E):
                out[-1] += cluster
            else:
                out.append(cluster)
    return out


def _attaches(character: str) -> bool:
    """A mark, a selector or a joiner, which joins the character before it."""
    return character == _JOINER or unicodedata.category(character).startswith("M")


def _noted_clusters(text: str, outside: bytes) -> list[tuple[str, bool]]:
    """A span's clusters, each with whether it must start a piece of its own. A new cluster starts wherever the outside notes change, so a
    name never shares one with the server's own text: a name that starts with a joiner, a selector, a keycap mark or a skin tone can't
    change an emoji or a # the server wrote before it, and one that ends in a joiner can't take the server's emoji after it. Where the text
    alone would have made one cluster across the change, the piece starts over as well, so the shaper never sees the two together. A mark,
    a selector or a joiner the server wrote right after outside text stays with it, since the server wrote it to change the letter before
    it. That also keeps a name's own marks on its letters when the notes cover only a name's plain letters."""
    if len(outside) != len(text) or len(set(outside)) < 2:
        return [(cluster, False) for cluster in _clusters(text)]
    seams = set(itertools.accumulate(map(len, _clusters(text))))  # where the text alone ends a cluster
    out: list[tuple[str, bool]] = []
    start = 0
    for end in range(1, len(text) + 1):
        if end == len(text) or (outside[end] != outside[start] and not (outside[start] and _attaches(text[end]))):
            out.extend((cluster, number == 0 and 0 < start and start not in seams) for number, cluster in enumerate(_clusters(text[start:end])))
            start = end
    return out


def _is_emoji_first(cluster: str) -> bool:
    first = cluster[0]
    if len(cluster) > 1 and any(character in _EMOJI_PARTS or 0x1F3FB <= ord(character) <= 0x1F3FF or 0x1F1E6 <= ord(character) <= 0x1F1FF for character in cluster):
        return True
    return 0x1F000 <= ord(first) <= 0x1FAFF or first in _EMOJI_BY_DEFAULT


def _wide(character: str) -> bool:
    """Scripts written without spaces, where a line can end after any character."""
    code = ord(character)
    return (0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFF60 or 0x20000 <= code <= 0x3134F or 0x3040 <= code <= 0x30FF
            or 0x0E00 <= code <= 0x0EFF)


def _direction(character: str) -> str:
    kind = unicodedata.bidirectional(character)
    if kind in ("R", "AL"):
        return "rtl"
    return "ltr" if kind == "L" else ""


def pieces_of(spans: list[Span], chain: Callable[[bool], tuple[fonts.Source, ...]]) -> tuple[list[list[Piece]], list[bool]]:
    """The spans as lines of pieces, and whether each line reads from the right. `chain(bold)` gives the fonts to try. A character no font
    has is drawn in its plain form when it has one, and left out otherwise."""
    lines: list[list[Piece]] = [[]]
    reads: list[bool | None] = [None]  # each line's direction, from its first letter that has one

    def push(piece: Piece, apart: bool = False) -> None:
        line = lines[-1]
        last = line[-1] if line else None
        if (last is not None and piece.kind == "text" and last.kind == "text" and not piece.breaks and not apart
                and (last.font, last.bold, last.italic, last.underline, last.rtl) == (piece.font, piece.bold, piece.italic, piece.underline, piece.rtl)):
            last.text += piece.text
        elif last is not None and piece.kind == "space" and last.kind == "space" and (last.underline, last.font) == (piece.underline, piece.font):
            last.text += piece.text
        else:
            line.append(piece)

    rtl = False
    for span in spans:
        sources = chain(span.bold)
        if span.emoji:
            lines[-1].append(Piece("custom", span.emoji, underline=span.underline, rtl=rtl, breaks=True))
            continue
        queue = _noted_clusters(span.text, span.outside)
        position = 0
        carry = False  # a cluster left out hands its place at the start of a piece to the one after it
        while position < len(queue):
            cluster, apart = queue[position]
            apart, carry = apart or carry, False
            position += 1
            if cluster == "\n":
                if len(lines) >= MAX_LINES:
                    return _with_directions(lines, reads)
                lines.append([])
                reads.append(None)
                rtl = False
                continue
            if cluster.isspace():
                push(Piece("space", " ", 0, span.bold, span.italic, span.underline, rtl))
                continue
            if all(_drawn_nothing(character) for character in cluster):
                carry = apart  # a joiner or a selector with nothing to join draws nothing, where the shaper would put a dotted circle
                continue
            strong = _direction(cluster[0])
            if strong:
                rtl = strong == "rtl"
                if reads[-1] is None:
                    reads[-1] = rtl
            last = lines[-1][-1] if lines[-1] else None
            after_break = last is None or last.kind in ("space", "emoji", "custom") or (last.kind == "text" and _wide(last.text[-1]))
            emoji_first = _is_emoji_first(cluster)
            if emoji_first and cards.emoji_picture(cluster) is not None:
                lines[-1].append(Piece("emoji", cluster, underline=span.underline, rtl=rtl, breaks=True))
                continue
            found = _font_for(sources, cluster)
            if found is None and not emoji_first and cards.emoji_picture(cluster) is not None:
                lines[-1].append(Piece("emoji", cluster, underline=span.underline, rtl=rtl, breaks=True))
                continue
            if found is None:
                plain = "".join(fonts.simpler(character) or ("" if fonts.pick(sources, character) is None else character) for character in cluster)
                plain = "".join(character for character in plain if character.isspace() or fonts.pick(sources, character) is not None)
                if plain and plain != cluster:
                    queue[position:position] = [(part, apart and not number) for number, part in enumerate(_clusters(plain))]
                else:
                    carry = apart
                continue
            push(Piece("text", cluster, found, span.bold, span.italic, span.underline, rtl, breaks=after_break or _wide(cluster[0])), apart)
    return _with_directions(lines, reads)


def _drawn_nothing(character: str) -> bool:
    """The joiner and the variation selectors, which a font may lack since they draw nothing of their own."""
    code = ord(character)
    return code == 0x200D or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF


def _font_for(sources: tuple[fonts.Source, ...], cluster: str) -> int | None:
    """Which font in the chain draws a cluster: the first that has every character of it, so a letter's marks are never drawn as empty boxes
    by a font that has the letter alone. Without one, the font that has the first character that draws something, or None when no font has
    it. A joiner that starts a name doesn't send the letter after it to another font."""
    needed = [character for character in cluster if not _drawn_nothing(character)]
    if len(needed) > 1:
        for index, source in enumerate(sources):
            if all(source.covers(character) for character in needed):
                return index
    return fonts.pick(sources, needed[0] if needed else cluster[0])


def _with_directions(lines: list[list[Piece]], reads: list[bool | None]) -> tuple[list[list[Piece]], list[bool]]:
    """A line with no letters to say which way it reads takes the direction of the first line that has one."""
    overall = next((read for read in reads if read is not None), False)
    return lines, [overall if read is None else read for read in reads]


# ---- measuring ----

@functools.lru_cache(maxsize=16384)
def _advance(source: fonts.Source, size: int, text: str, rtl: bool) -> float:
    for shaped in (True, False):
        try:
            if shaped and fonts.RAQM:
                return float(source.at(size).getlength(text, direction="rtl" if rtl else "ltr"))
            return float(source.at(size, shaped=False).getlength(text))
        except RuntimeError:  # the shaper refused the text, so it is measured as _write then draws it, without shaping
            continue
        except (OSError, ValueError, KeyError):
            break
    return float(size) * 0.6 * len(text)


def _write(pen: ImageDraw.ImageDraw, at: tuple[float, float], letters: str, source: fonts.Source, size: int, rtl: bool, stroke: int) -> None:
    """Draws letters with the font's shaping, or one after another without it when the shaper refuses them, so the text is still drawn.
    Without shaping, right-to-left letters are laid from the right, as the shaper lays them."""
    if fonts.RAQM:
        try:
            pen.text(at, letters, font=source.at(size), fill=255, anchor="ls", stroke_width=stroke, direction="rtl" if rtl else "ltr")
            return
        except RuntimeError:
            pass
    if rtl:
        letters = "".join(reversed(_clusters(letters)))
    pen.text(at, letters, font=source.at(size, shaped=False), fill=255, anchor="ls", stroke_width=stroke)


@dataclass
class Setting:
    """How one text layer wants its text set."""
    family: str = fonts.DEFAULT
    weight: int = 400
    size: float = 48
    line_height: float = 1.2
    letter_spacing: float = 0
    align: str = "left"
    valign: str = "top"
    wrap: bool = True
    shrink: bool = True
    outline: int = 0


@dataclass
class Placed:
    piece: Piece
    x: float
    width: float


@dataclass
class Line:
    placed: list[Placed] = field(default_factory=list)
    width: float = 0.0


@dataclass
class Layout:
    size: int
    lines: list[Line]
    advance: float       # from one baseline to the next
    ascent: float
    descent: float
    top: float           # where the first line's box starts inside the layer's box
    cut: bool = False    # some text was left out because the box is too small for it at the smallest size
    shrunk: bool = False


class _Setter:
    """Measures and breaks one text at one size."""

    def __init__(self, setting: Setting, chains: dict[bool, tuple[fonts.Source, ...]], size: int) -> None:
        self.setting = setting
        self.chains = chains
        self.size = size
        self.spacing = setting.letter_spacing

    def source(self, piece: Piece) -> fonts.Source:
        return self.chains[piece.bold][piece.font]

    def width(self, piece: Piece) -> float:
        if piece.kind == "emoji":
            return cards.emoji_advance(piece.text, self.size) + self.spacing
        if piece.kind == "custom":
            return self.size * (EMOJI_HEIGHT + 2 * EMOJI_SPACE) + self.spacing
        source = self.source(piece)
        if self.spacing:
            return sum(_advance(source, self.size, cluster, piece.rtl) + self.spacing for cluster in _clusters(piece.text))
        extra = 2 * thickening(self.size) if source.thicken and piece.kind == "text" else 0
        return _advance(source, self.size, piece.text, piece.rtl) + extra

    def split(self, piece: Piece, room: float) -> tuple[Piece, Piece | None]:
        """As much of a text piece as fits the room, and the rest. At least one character always goes in, so a narrow box still makes progress."""
        parts = _clusters(piece.text)
        keep = 1
        while keep < len(parts) and self.width(_copy(piece, "".join(parts[:keep + 1]))) <= room:
            keep += 1
        head = _copy(piece, "".join(parts[:keep]))
        tail = _copy(piece, "".join(parts[keep:]), breaks=True) if keep < len(parts) else None
        return head, tail

    def lines(self, pieces: list[Piece], room: float, *, wrap: bool) -> tuple[list[list[Piece]], bool]:
        """One written line as the lines it takes in the room, and whether a word had to be cut in the middle to fit."""
        if not wrap:
            return [_trimmed(pieces)], False
        out: list[list[Piece]] = [[]]
        used = 0.0
        forced = False
        pending = list(pieces)
        index = 0
        while index < len(pending):
            # A word is the pieces up to the next place a line can end.
            end = index + 1
            while end < len(pending) and not pending[end].breaks and pending[end].kind != "space" and pending[index].kind != "space":
                end += 1
            word = pending[index:end]
            width = sum(self.width(piece) for piece in word)
            if word[0].kind == "space":
                if out[-1]:
                    out[-1].extend(word)
                    used += width
                index = end
                continue
            if used + width <= room or not out[-1] and width <= room:
                out[-1].extend(word)
                used += width
                index = end
                continue
            if out[-1]:
                out[-1] = _trimmed(out[-1])
                out.append([])
                used = 0.0
                if len(out) > MAX_LINES:
                    out.pop()
                    return out, forced
                continue
            # The word alone is wider than the room, so it is cut wherever it fills up.
            forced = True
            first = word[0]
            if first.kind != "text":
                out[-1].append(first)
                used += self.width(first)
                index += 1
                continue
            head, tail = self.split(first, room - used)
            out[-1].append(head)
            used += self.width(head)
            if tail is None:
                index += 1
            else:
                pending[index] = tail
                out.append([])
                used = 0.0
                if len(out) > MAX_LINES:
                    out.pop()
                    return out, forced
        out[-1] = _trimmed(out[-1])
        return out, forced


def _copy(piece: Piece, text: str, *, breaks: bool | None = None) -> Piece:
    return Piece(piece.kind, text, piece.font, piece.bold, piece.italic, piece.underline, piece.rtl, piece.breaks if breaks is None else breaks)


def _trimmed(pieces: list[Piece]) -> list[Piece]:
    end = len(pieces)
    while end and pieces[end - 1].kind == "space":
        end -= 1
    return pieces[:end]


def thickening(size: float) -> int:
    """How much thicker bold letters are drawn in a font with no heavier weight."""
    return max(1, round(size / 48))


def _visual(pieces: list[Piece], base_rtl: bool) -> list[Piece]:
    """The pieces in the order they're drawn from the left. A run against the line's direction keeps its own order inside the line."""
    if not base_rtl and not any(piece.rtl for piece in pieces if piece.kind == "text"):
        return pieces
    # A space or an emoji has no direction of its own. Between two words that read the same way it goes with them, and anywhere else it
    # follows the line.
    letters = [piece.rtl if piece.kind == "text" else None for piece in pieces]
    before: list[bool | None] = []
    last: bool | None = None
    for value in letters:
        last = value if value is not None else last
        before.append(last)
    after: list[bool | None] = []
    last = None
    for value in reversed(letters):
        last = value if value is not None else last
        after.append(last)
    after.reverse()
    reads = [value if value is not None else (before[index] if before[index] is not None and before[index] == after[index] else base_rtl) for index, value in enumerate(letters)]
    runs: list[tuple[bool, list[Piece]]] = []
    for piece, rtl in zip(pieces, reads):
        if runs and runs[-1][0] == rtl:
            runs[-1][1].append(piece)
        else:
            runs.append((rtl, [piece]))
    out: list[Piece] = []
    for rtl, run in (reversed(runs) if base_rtl else runs):
        out.extend(reversed(run) if rtl else run)
    return out


def layout(marked: Marked, setting: Setting, width: int, height: int) -> Layout:
    """Where every piece of a text goes inside a box of this size."""
    chains = {False: fonts.chain(setting.family, setting.weight, False), True: fonts.chain(setting.family, setting.weight, True)}
    written, bases = pieces_of(parse(marked), lambda bold: chains[bold])
    asked = max(1, round(setting.size))
    floor = min(asked, max(SMALLEST, round(asked * SMALLEST_SHARE)))

    def attempt(size: int) -> tuple[list[tuple[list[Piece], bool]], bool, _Setter]:
        setter = _Setter(setting, chains, size)
        rows: list[tuple[list[Piece], bool]] = []
        forced = False
        for pieces, base in zip(written, bases):
            broken, cut_word = setter.lines(pieces, width, wrap=setting.wrap)
            forced = forced or cut_word
            rows.extend((row, base) for row in broken)
        return rows, forced, setter

    def fits(size: int) -> bool:
        rows, forced, setter = attempt(size)
        if forced or len(rows) * size * setting.line_height > height + 0.5:
            return False
        return all(sum(setter.width(piece) for piece in row) <= width + 0.5 for row, _ in rows)

    size = asked
    if setting.shrink and not fits(asked):
        low, high = floor, asked
        while low < high:  # the largest size that fits, since a smaller size never needs more room
            middle = (low + high + 1) // 2
            if fits(middle):
                low = middle
            else:
                high = middle - 1
        size = low
    rows, _, setter = attempt(size)
    advance = size * setting.line_height
    cut = False
    if setting.shrink:
        room = max(1, int((height + 0.5) // advance))
        if len(rows) > room:
            rows, cut = rows[:room], True
    primary = chains[False][0].at(size)
    ascent, descent = primary.getmetrics()
    lines: list[Line] = []
    for number, (row, base) in enumerate(rows):
        row = list(row)
        last = number == len(rows) - 1
        if setting.shrink and (cut and last or sum(setter.width(piece) for piece in row) > width + 0.5):
            row, was_cut = _ellipsized(row, setter, width, chains, always=cut and last)
            cut = cut or was_cut
        line = Line()
        cursor = 0.0
        for piece in _visual(row, base):
            piece_width = setter.width(piece)
            line.placed.append(Placed(piece, cursor, piece_width))
            cursor += piece_width
        line.width = max(0.0, cursor - (setting.letter_spacing if row else 0))
        lines.append(line)
    block = len(lines) * advance
    top = 0.0 if setting.valign == "top" else (height - block) / 2 if setting.valign == "middle" else height - block
    for line in lines:
        shift = 0.0 if setting.align == "left" else (width - line.width) / 2 if setting.align == "center" else width - line.width
        for placed in line.placed:
            placed.x += shift
    return Layout(size, lines, advance, float(ascent), float(descent), top, cut, size < asked)


def _ellipsized(row: list[Piece], setter: _Setter, width: float, chains: dict, *, always: bool) -> tuple[list[Piece], bool]:
    """A line cut to fit with an ellipsis at its end. `always` adds the ellipsis even when the line fits, to say more text followed."""
    def total(pieces: list[Piece]) -> float:
        return sum(setter.width(piece) for piece in pieces)

    if not always and total(row) <= width + 0.5:
        return row, False
    sources = chains[False]
    mark = ELLIPSIS if sources[0].covers(ELLIPSIS) else "..."
    row = _trimmed(list(row))
    template = next((piece for piece in reversed(row) if piece.kind == "text"), None)
    dots = Piece("text", mark, 0, False, False, template.underline if template else False, False)
    while row and total(row + [dots]) > width + 0.5:
        last = row[-1]
        parts = _clusters(last.text) if last.kind == "text" else []
        if len(parts) > 1:
            row[-1] = _copy(last, "".join(parts[:-1]))
        else:
            row.pop()
            row = _trimmed(row)
    return row + [dots], True


# ---- drawing ----

def _keep_going() -> None:
    """The time check of a caller that sets no deadline."""


def paint(found: Layout, setting: Setting, width: int, height: int, pad: int, pictures: Callable[[str], Image.Image | None],
          check: Callable[[], None] = _keep_going) -> tuple[Image.Image, Image.Image | None, Image.Image | None]:
    """The text as three images the size of the box plus `pad` on every side: the letters' ink, the outline's ink when there is one, and the
    emoji in color when there are any. `pictures(ID)` gives a server emoji's picture.

    Only what lands in those images is drawn. A line that runs far past the box, as one with wrapping and shrinking off can, is cut to the
    letters that show, so drawing it costs no more than its picture. `check` runs between the pieces and raises when the time is up."""
    size = found.size
    full = (width + 2 * pad, height + 2 * pad)
    ink = Image.new("L", full, 0)
    outline = Image.new("L", full, 0) if setting.outline else None
    color: Image.Image | None = None
    ink_draw = ImageDraw.Draw(ink)
    outline_draw = ImageDraw.Draw(outline) if outline is not None else None
    chains = {False: fonts.chain(setting.family, setting.weight, False), True: fonts.chain(setting.family, setting.weight, True)}
    line_box = found.ascent + found.descent
    under = max(1, round(size / 14))
    # How far a letter's ink can reach outside its own box: its outline, bold made by thickening, the lean of a slanted letter and the parts
    # that hang past the box, as in a script face. A piece farther than this outside the images puts no ink in them.
    reach = setting.outline + thickening(size) + size + 2
    for number, line in enumerate(found.lines):
        baseline = pad + found.top + number * found.advance + (found.advance - line_box) / 2 + found.ascent
        if baseline - found.ascent - reach > full[1] or baseline + found.descent + reach < 0:
            continue
        for placed in line.placed:
            check()
            piece = placed.piece
            x = pad + placed.x
            start, end = sorted((x, x + placed.width))  # letter spacing below zero can pull a piece narrower than nothing, leftward from x
            if start - reach > full[0] or end + reach < 0:
                continue
            if piece.underline:
                box = (start, baseline + size * 0.1, end, baseline + size * 0.1 + under)
                ink_draw.rectangle(box, fill=255)
                if outline_draw is not None:
                    outline_draw.rectangle((box[0] - setting.outline, box[1] - setting.outline, box[2] + setting.outline, box[3] + setting.outline), fill=255)
            if piece.kind in ("emoji", "custom"):
                picture = cards.emoji_picture(piece.text) if piece.kind == "emoji" else pictures(piece.text)
                if picture is None:
                    continue
                tall = size * EMOJI_HEIGHT
                wide = tall * picture.width / picture.height if piece.kind == "emoji" else tall
                scaled = _fitted(picture, max(1, round(wide)), max(1, round(tall)))
                if color is None:
                    color = Image.new("RGBA", full, (0, 0, 0, 0))
                middle = baseline - size * 0.36  # where capital letters have their middle
                stamp(color, scaled, round(x + size * EMOJI_SPACE), round(middle - scaled.height / 2))
                continue
            if piece.kind != "text":
                continue
            source = chains[piece.bold][piece.font]
            thick = thickening(size) if source.thicken else 0
            shown = _shown(piece, source, size, x, placed.width, setting.letter_spacing, -reach, full[0] + reach)
            if shown is None:
                continue
            piece, x = shown
            if piece.italic:
                _slanted(ink, outline, piece, source, x, baseline, size, thick, setting, pad, check)
                continue
            for letters, at, advance in _spaced(piece, source, size, x, setting.letter_spacing):
                if advance is not None and (at - reach > full[0] or at + advance + reach < 0):
                    continue  # a spaced letter wholly outside, as letter spacing below zero can leave a piece that is handed back whole
                check()
                _write(ink_draw, (at + thick, baseline), letters, source, size, piece.rtl, thick)
                if outline_draw is not None:
                    _write(outline_draw, (at + thick, baseline), letters, source, size, piece.rtl, thick + setting.outline)
    return ink, outline, color


def _shown(piece: Piece, source: fonts.Source, size: int, x: float, width: float, spacing: float, low: float, high: float) -> tuple[Piece, float] | None:
    """The part of a text piece at `x` that can put ink between `low` and `high`, and where that part starts. None when no part can.

    The clusters are taken in the order they are drawn, left to right, which for right-to-left text is the reverse of reading order. Without
    letter spacing, where the kept part starts is the whole piece's length less the length of what follows the dropped part, so a kerning pair
    across the cut keeps the kept letters where the whole piece puts them."""
    if min(x, x + width) >= low and max(x, x + width) <= high:
        return piece, x
    parts = _clusters(piece.text)
    order = parts[::-1] if piece.rtl else parts
    count = len(order)
    known: dict[int, float] = {0: 0.0}
    if spacing:
        for at, cluster in enumerate(order):
            known[at + 1] = known[at] + _advance(source, size, cluster, piece.rtl) + spacing
        if any(known[at + 1] < known[at] for at in range(count)):
            # Letter spacing below a letter's width puts it left of the one before, so the search below has no order to go by. Each
            # spaced letter is drawn on its own, and what lands off the picture costs little.
            return piece, x

        def left(drawn: int) -> float:
            return known[drawn]
    else:
        whole = _advance(source, size, piece.text, piece.rtl)
        known[count] = whole

        def left(drawn: int) -> float:
            """Where the cluster drawn `drawn`-th from the left starts, from the piece's left edge."""
            if drawn not in known:
                rest = "".join(parts[:count - drawn]) if piece.rtl else "".join(parts[drawn:])
                known[drawn] = whole - _advance(source, size, rest, piece.rtl)
            return known[drawn]
    # Drop the clusters that end at or before `low`, and those that start at or past `high`. A piece's length grows with its clusters.
    first, last = 0, count
    while first < last:
        middle = (first + last + 1) // 2
        if left(middle) <= low - x:
            first = middle
        else:
            last = middle - 1
    start, end = first, count
    while start < end:
        middle = (start + end) // 2
        if left(middle) >= high - x:
            end = middle
        else:
            start = middle + 1
    if first >= end:
        return None
    kept = order[first:end]
    return _copy(piece, "".join(kept[::-1] if piece.rtl else kept)), x + left(first)


def _spaced(piece: Piece, source: fonts.Source, size: int, x: float, spacing: float) -> list[tuple[str, float, float | None]]:
    """The piece as the strings to draw, where each starts and how far it advances. With letter spacing, each character is drawn on its own.
    Without it the piece is one string, already cut to what shows, with no advance given."""
    if not spacing:
        return [(piece.text, x, None)]
    out = []
    parts = _clusters(piece.text)
    for cluster in (reversed(parts) if piece.rtl else parts):
        advance = _advance(source, size, cluster, piece.rtl)
        out.append((cluster, x, advance))
        x += advance + spacing
    return out


def _slanted(ink: Image.Image, outline: Image.Image | None, piece: Piece, source: fonts.Source, x: float, baseline: float, size: int, thick: int,
             setting: Setting, pad: int, check: Callable[[], None] = _keep_going) -> None:
    """Draws a piece leaning to the right. The fonts here have no italic files, so the upright letters are drawn on a tile and the tile is sheared about the baseline."""
    margin = setting.outline + thick + int(size * 0.6) + 2
    parts = [piece.text] if not setting.letter_spacing else (list(reversed(_clusters(piece.text))) if piece.rtl else _clusters(piece.text))
    # Where each part starts and how far it reaches, from where the piece starts. Letter spacing below a letter's width puts a part left of
    # the one before it, so the order says nothing of where a part lies.
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for letters in parts:
        check()
        advance = _advance(source, size, letters, piece.rtl)
        spans.append((cursor, cursor + advance))
        cursor += advance + setting.letter_spacing
    # Only the parts that can put ink in the picture are drawn, so a line that runs far past it costs no more than the picture. A part's
    # ink, slant included, stays within the margin around its span.
    shown = [number for number, (start, reach) in enumerate(spans) if x + start - margin < ink.width and x + reach + margin > 0]
    if not shown:
        return
    # The tile starts a whole number of pixels from where the whole piece's tile would, so each letter lands where it would on that tile,
    # to the fraction of a pixel.
    first = min(start for start, _ in spans)
    origin = first + math.floor(min(spans[number][0] for number in shown) - first)
    farthest = max(spans[number][1] for number in shown)
    if shown[-1] == len(parts) - 1:
        farthest = max(farthest, cursor)  # the spacing after the last part, as the tile always had
    wide = int(farthest - origin) + 2 * margin
    tall = int(size * 2.2) + 2 * margin
    base = margin + int(size * 1.4)
    for target, stroke in ((ink, thick), (outline, thick + setting.outline)):
        if target is None:
            continue
        check()
        tile = Image.new("L", (wide, tall), 0)
        pen = ImageDraw.Draw(tile)
        for number in shown:
            check()
            _write(pen, (margin - origin + spans[number][0] + thick, base), parts[number], source, size, piece.rtl, stroke)
        leaned = tile.transform(tile.size, Image.Transform.AFFINE, (1, ITALIC_SLANT, -ITALIC_SLANT * base, 0, 1, 0), resample=Image.Resampling.BICUBIC)
        left, top = round(x - margin + first) + int(origin - first), round(baseline - base)
        _lighten(target, leaned, left, top)


def _lighten(target: Image.Image, tile: Image.Image, left: int, top: int) -> None:
    """Adds a tile's ink to a mask, keeping whichever is stronger where they overlap, and leaving out what falls off the mask."""
    box = (max(0, left), max(0, top), min(target.width, left + tile.width), min(target.height, top + tile.height))
    if box[0] >= box[2] or box[1] >= box[3]:
        return
    part = tile.crop((box[0] - left, box[1] - top, box[2] - left, box[3] - top))
    target.paste(ImageChops.lighter(target.crop(box), part), box[:2])


def stamp(target: Image.Image, tile: Image.Image, left: int, top: int) -> None:
    """Lays a see-through tile over a picture at any place, leaving out what falls off the edge."""
    box = (max(0, left), max(0, top), min(target.width, left + tile.width), min(target.height, top + tile.height))
    if box[0] >= box[2] or box[1] >= box[3]:
        return
    target.alpha_composite(tile, (box[0], box[1]), (box[0] - left, box[1] - top, box[2] - left, box[3] - top))


def _fitted(picture: Image.Image, wide: int, tall: int) -> Image.Image:
    """A picture scaled to sit inside the box, keeping its shape, on a clear tile of exactly that box."""
    scale = min(wide / picture.width, tall / picture.height)
    size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
    scaled = picture.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    tile = Image.new("RGBA", (wide, tall), (0, 0, 0, 0))
    tile.alpha_composite(scaled, ((wide - size[0]) // 2, (tall - size[1]) // 2))
    return tile
