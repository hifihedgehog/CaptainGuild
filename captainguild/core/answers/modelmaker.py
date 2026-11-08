"""Printable models for /make_model.

The model writes the part in OpenSCAD and calls render_model, which runs OpenSCAD on this machine to make a 3MF file and a preview
picture, and reports the finished size back so the model can fix mistakes before it replies. The member gets the 3MF, the editable
.scad source and the preview.

OpenSCAD runs on code a model wrote from what a member typed, so each render is held in: a fresh temporary folder, an account with no
login when the bot runs as root, limits on memory, CPU time and file size through prlimit, a time limit, a process group that dies
whole, and none of OpenSCAD's ways to read a file (include and use, import and its older forms, surface, the DXF functions, and the
file the extrusions can take), so the code can't read files from the machine. It needs the openscad, xvfb-run and prlimit programs, and
without them /make_model says it's unavailable.
"""
from __future__ import annotations

import asyncio
import io
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import zipfile
from typing import Any, NamedTuple

from .engine import Answer

MAX_ROUNDS = 8
MAX_TOKENS = 64000  # the OpenSCAD source and the thinking before it, which at Max effort passed 20,000 tokens on a plain request
MAX_RENDERS = 4  # renders one answer runs: a first try and three fixes, each up to six and a half minutes
# Renders run as this account when the bot runs as root, and as nobody when it doesn't exist.
ACCOUNT = "makemodel"

RENDER_SECONDS = 150  # the full render to a mesh, and again for the 3MF
PREVIEW_SECONDS = 90
RENDER_MEMORY = 4 << 30  # bytes of address space
RENDER_CPU = 180  # CPU seconds
RENDER_FILE_SIZE = 200 << 20  # the largest file the renderer may write
SCAD_MAX_CHARS = 60_000
MODEL_FILE_MAX_BYTES = 8_000_000  # leaves room under Discord's 10 MB upload limit
PROGRAMS = ("openscad", "xvfb-run", "prlimit")

# Every way OpenSCAD 2021.01 reads a file or a path, from its source: include <x> and use <x>, which its lexer reads wherever it meets
# them (src/lexer.l:142 and :151), the modules import, import_stl, import_off and import_dxf (src/import.cc:247-251), surface
# (src/surface.cc:329), dxf_linear_extrude and dxf_rotate_extrude (src/linearextrude.cc:185, src/rotateextrude.cc:133), the functions
# dxf_dim and dxf_cross (src/dxfdim.cc:249-254), and linear_extrude and rotate_extrude given a file, which is their first parameter, so
# an unnamed argument fills it too (src/linearextrude.cc:59 and :80, src/rotateextrude.cc:55 and :71). linear_extrude reads only a
# string there and takes a number as the height, while rotate_extrude reads whatever it's given.
_READERS = frozenset({"import", "import_stl", "import_off", "import_dxf", "surface", "dxf_linear_extrude", "dxf_rotate_extrude", "dxf_dim",
                      "dxf_cross"})
_EXTRUDES = {
    "linear_extrude": "linear_extrude reads a file given as file = or as an unnamed argument that isn't a number, and code here can't read "
    "files. Name its arguments, such as linear_extrude(height = 5).",
    "rotate_extrude": "rotate_extrude reads a file given as file = or as an unnamed argument, and code here can't read files. Name its "
    "arguments, such as rotate_extrude(angle = 360).",
}
# The code is read the way OpenSCAD's lexer reads it (src/lexer.l), so a comment, a line break or a no-break space between a name and its
# bracket doesn't hide a call, and a name inside a string or a comment isn't one. These are its rules for the tokens that matter here.
_LIBRARY = re.compile(r"(?:include|use)[ \t\r\n]*<", re.I)
_NAME = re.compile(r"\$?[A-Za-z0-9_]+")
_NUMBERS = tuple(re.compile(pattern) for pattern in (r"[0-9]+(?:[Ee][+-]?[0-9]+)?", r"[0-9]*\.[0-9]+(?:[Ee][+-]?[0-9]+)?",
                                                     r"[0-9]+\.[0-9]*(?:[Ee][+-]?[0-9]+)?"))
_PAIRS = ("<=", ">=", "==", "!=", "&&", "||")
_OPENING = (("mark", "("), ("mark", "["), ("mark", "{"))
_CLOSING = (("mark", ")"), ("mark", "]"), ("mark", "}"))
_SPACE = " \t\r\n\u00a0"  # the lexer skips a no-break space as it does a space (src/lexer.l:244)
# A string's escapes and what the lexer turns them into (src/lexer.l:171-178).
_ESCAPED = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
_HEX_BYTE = re.compile(r"\\x([0-7][0-9A-Fa-f])")
_CODE_POINT = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{6})")
Token = tuple[str, str]
_COMMA, _COLON, _ASSIGN = ("mark", ","), ("mark", ":"), ("mark", "=")
_ENDS = (("mark", ";"), ("mark", "}"))  # what comes before a statement at the top level
_SIGNS = (("mark", "-"), ("mark", "+"))
UINT32 = 2 ** 32
RANGE_STEPS = 10_000  # chr() reads nothing from a range with this many values or more (src/value.h:49)
TEXT_BUDGET = 100_000  # characters chr() and str() may build for the screen in one model, counting each value chr() reads
TEXT_DEPTH = 64  # vectors, calls and names one built text may nest through
NO_CHR = 'chr() takes only numbers, lists of numbers and ranges of numbers here. Write the text as a string, such as text("Hello").'
TOO_LONG = f"chr() and str() here build more text than can be checked, over {TEXT_BUDGET:,} characters. Write it as plain strings."
TOO_DEEP = f"chr() and str() here nest deeper than can be checked, past {TEXT_DEPTH} levels. Write the text as plain strings."


class _Range(NamedTuple):
    begin: float
    step: float
    end: float


_VERTEX = re.compile(rb'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"')


def available() -> bool:
    return all(shutil.which(program) for program in PROGRAMS)


def _sandbox_account() -> tuple[int, int] | None:
    """The user and group IDs renders run as, or None when this process can't switch accounts."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    import pwd

    try:
        entry = pwd.getpwnam(ACCOUNT)
    except KeyError:
        entry = pwd.getpwnam("nobody")
    return entry.pw_uid, entry.pw_gid


def run_held(command: list[str], folder: str, seconds: int) -> subprocess.CompletedProcess:
    """Run a command held in by the limits above. On a timeout the whole process group dies, so the X server xvfb-run starts for a
    preview can't outlive it."""
    limits = ["prlimit", f"--as={RENDER_MEMORY}", f"--cpu={RENDER_CPU}", f"--fsize={RENDER_FILE_SIZE}", "--"]
    env = {"HOME": folder, "XDG_RUNTIME_DIR": folder, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    options: dict[str, Any] = {}
    account = _sandbox_account()
    if account is not None:
        options = {"user": account[0], "group": account[1], "extra_groups": []}
    process = subprocess.Popen(limits + command, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True, **options)
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def mesh_stats(model_3mf: bytes) -> tuple[tuple[float, ...], tuple[float, ...], int]:
    """The corners of a 3MF file's bounding box, and its triangle count."""
    with zipfile.ZipFile(io.BytesIO(model_3mf)) as archive:
        xml = archive.read("3D/3dmodel.model")
    lows, highs = [float("inf")] * 3, [float("-inf")] * 3
    for match in _VERTEX.finditer(xml):
        for axis in range(3):
            value = float(match.group(axis + 1))
            lows[axis] = min(lows[axis], value)
            highs[axis] = max(highs[axis], value)
    if lows[0] == float("inf"):
        raise ValueError("The rendered model is empty. Check that the code produces a solid.")
    return tuple(lows), tuple(highs), xml.count(b"<triangle ")


def _tokens(scad: str):
    """The code's tokens as OpenSCAD's lexer reads them, as (kind, text), leaving out spaces and comments. It stops at include <x> or
    use <x>, which the lexer reads as it meets them."""
    at, end = 0, len(scad)
    while at < end:
        char = scad[at]
        if char in _SPACE:
            at += 1
        elif scad.startswith("//", at):  # to the end of the line, which a carriage return alone doesn't end
            newline = scad.find("\n", at)
            at = end if newline < 0 else newline + 1
        elif scad.startswith("/*", at):
            close = scad.find("*/", at + 2)
            at = end if close < 0 else close + 2
        elif char == '"':  # a backslash escapes the next character, a quote among them, and a string may run over lines
            start, at = at, at + 1
            while at < end and scad[at] != '"':
                at += 2 if scad[at] == "\\" else 1
            at += 1
            yield "string", scad[start:at]
        elif library := _LIBRARY.match(scad, at):
            yield "library", library.group()
            return
        else:
            # The longest match wins, and a number wins a tie, so 1e5 is a number, 1e5x a name, and 1.5import a number and a name.
            name = _NAME.match(scad, at)
            number = max((len(match.group()) for pattern in _NUMBERS if (match := pattern.match(scad, at))), default=0)
            if name and len(name.group()) > number:
                yield "name", name.group()
                at = name.end()
            elif number:
                yield "number", scad[at:at + number]
                at += number
            else:
                width = 2 if scad[at:at + 2] in _PAIRS else 1
                yield "mark", scad[at:at + width]
                at += width


def _closings(tokens: list[Token]) -> dict[int, int]:
    """Where each bracket closes."""
    closing: dict[int, int] = {}
    opened: list[int] = []
    for at, token in enumerate(tokens):
        if token in _OPENING:
            opened.append(at)
        elif token in _CLOSING and opened:
            closing[opened.pop()] = at
    return closing


def _parts(tokens: list[Token], start: int, end: int, closing: dict[int, int], separator: Token) -> list[tuple[int, int]]:
    """Where each part of tokens[start:end] between separators outside brackets starts and ends, empty parts included. A bracket stands
    for everything up to its closing bracket, so each token is read once however deep the brackets nest."""
    parts: list[tuple[int, int]] = []
    first = at = start
    while at < end:
        if tokens[at] == separator:
            parts.append((first, at))
            first = at + 1
        elif tokens[at] in _OPENING:
            at = closing.get(at, end)
        at += 1
    parts.append((first, end))
    return parts


def _argument_spans(tokens: list[Token], opening: int, closing: dict[int, int]) -> list[tuple[int, int]]:
    """Where each argument of the call whose bracket is at tokens[opening] starts and ends, without empty ones."""
    end = closing.get(opening, len(tokens))
    return [(first, last) for first, last in _parts(tokens, opening + 1, end, closing, _COMMA) if last > first]


def _call_opening(tokens: list[Token], at: int) -> int | None:
    """Where the bracket of a call to the name at tokens[at] is, or None when the name isn't called. A number between them doesn't
    count, since the lexer drops a number too large for a double (src/lexer.l:248-254)."""
    opening = at + 1
    while opening < len(tokens) and tokens[opening][0] == "number":
        opening += 1
    return opening if opening < len(tokens) and tokens[opening] == ("mark", "(") else None


def _number(tokens: list[Token], start: int, end: int) -> float | None:
    """The value of tokens[start:end] when it's a number, signed or not, and None when it's anything else."""
    if end - start == 1 and tokens[start][0] == "number":
        return float(tokens[start][1])
    if end - start == 2 and tokens[start] in _SIGNS and tokens[start + 1][0] == "number":
        return float(tokens[start][1] + tokens[start + 1][1])
    return None


def _named(tokens: list[Token], start: int, end: int) -> bool:
    return end - start > 1 and tokens[start][0] == "name" and tokens[start + 1] == _ASSIGN


def file_refusal(scad: str) -> str:
    """Words for the model when the code would read a file, and "" when it wouldn't."""
    tokens = list(_tokens(scad))
    closing = _closings(tokens)
    for at, (kind, text) in enumerate(tokens):
        if kind == "library":
            return "include and use read other files, and code here can't read files. Build the part from primitives."
        if kind != "name":
            continue
        word = text.lower()
        if word in _READERS:
            return f"{text} reads a file, and code here can't read files. Build the part from primitives."
        opening = _call_opening(tokens, at) if word in _EXTRUDES else None
        if opening is None:
            continue
        for start, end in _argument_spans(tokens, opening, closing):
            named = _named(tokens, start, end)
            if named and tokens[start][1].lower() == "file":
                return _EXTRUDES[word]
            if not named and (word == "rotate_extrude" or _number(tokens, start, end) is None):
                return _EXTRUDES[word]
    return ""


def _character(code: int) -> str:
    """The character OpenSCAD makes of a code point, or "" for zero, a surrogate or a value past U+10FFFF, which GLib's
    g_unichar_validate refuses."""
    return chr(code) if 0 < code < 0x110000 and not 0xD800 <= code <= 0xDFFF else ""


def _uint32(value: float) -> int:
    """A double converted to a 32-bit unsigned value the way x86-64 builds convert it: the low 32 bits of its whole part, or 0 when
    that doesn't fit in 64 bits."""
    if math.isnan(value) or not -(2.0 ** 63) < value < 2.0 ** 63:
        return 0
    return int(value) % UINT32


def _range_count(found: _Range) -> int:
    """How many values a range has (src/value.cc:1157-1177)."""
    begin, step, end = found
    if math.isnan(begin) or math.isnan(step) or math.isnan(end):
        return 0
    if (begin < end) if step < 0 else (begin > end):
        return 0
    if begin == end or math.isinf(step):
        return 1
    if math.isinf(begin) or math.isinf(end) or step == 0:
        return UINT32 - 1
    steps = _uint32(math.nextafter((end - begin) / step, UINT32 - 1))
    return UINT32 - 1 if steps == UINT32 - 1 else steps + 1


def _range_values(found: _Range) -> list[float]:
    """The values chr() reads from a range: none when it has RANGE_STEPS values or more (src/value.cc:517-521) or its step is 0, which
    ends the iterator at once (value.cc:1185-1188)."""
    count = _range_count(found)
    if count >= RANGE_STEPS or found.step == 0:
        return []
    return [found.begin + found.step * index for index in range(count)]


class _Built:
    """Works out the text chr() and str() calls build, the way OpenSCAD 2021.01 builds it, within TEXT_BUDGET and TEXT_DEPTH."""

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.closing = _closings(tokens)
        self.spent = 0
        self.spelled: dict[int, str | None] = {}
        self.joined: dict[int, str] = {}
        self.joining: set[int] = set()
        self.names: dict[str, str | None] = {}
        self.resolving: set[str] = set()
        self.depths: list[int] = []  # how many brackets each token sits inside
        self.assigned: dict[str, list[int]] = {}  # where each name is given a value: by an assignment, a parameter or a named argument
        depth = 0
        for at, token in enumerate(tokens):
            self.depths.append(depth)
            if token in _OPENING:
                depth += 1
            elif token in _CLOSING:
                depth = max(depth - 1, 0)
            if token[0] == "name" and at + 1 < len(tokens) and tokens[at + 1] == _ASSIGN:
                self.assigned.setdefault(token[1], []).append(at)

    def spend(self, amount: int) -> None:
        self.spent += amount
        if self.spent > TEXT_BUDGET:
            raise ValueError(TOO_LONG)

    def literal(self, start: int, end: int, depth: int) -> float | list | _Range | None:
        """tokens[start:end] as a number, a vector of literals or a range of numbers, and None when it's anything else."""
        if depth > TEXT_DEPTH:
            raise ValueError(TOO_DEEP)
        number = _number(self.tokens, start, end)
        if number is not None:
            return number
        if end - start < 2 or self.tokens[start] != ("mark", "[") or self.closing.get(start) != end - 1:
            return None
        bounds = _parts(self.tokens, start + 1, end - 1, self.closing, _COLON)
        if len(bounds) > 1:  # [begin:end] or [begin:step:end] (src/parser.y:511-518)
            numbers = [_number(self.tokens, first, last) for first, last in bounds]
            if len(numbers) > 3 or None in numbers:
                return None
            if len(numbers) == 3:
                return _Range(*numbers)
            low, high = sorted(numbers)  # without a step, a range written backward runs forward (src/expr.cc:272-277)
            return _Range(low, 1.0, high)
        items = []
        for first, last in _parts(self.tokens, start + 1, end - 1, self.closing, _COMMA):
            if last > first:  # a vector takes extra commas (parser.y:519-523)
                item = self.literal(first, last, depth + 1)
                if item is None:
                    return None
                items.append(item)
        return items

    def chr_text(self, value: float | list | _Range, depth: int) -> str:
        """The text chr() makes of a literal (src/value.cc:485-527): the code point of a positive number's whole part, kept to 32 bits,
        each item of a vector, and each value of a range."""
        if depth > TEXT_DEPTH:
            raise ValueError(TOO_DEEP)
        if isinstance(value, float):
            self.spend(1)
            return _character(_uint32(value) if value > 0 else 0)
        if isinstance(value, _Range):
            values = _range_values(value)
            self.spend(len(values))
            return "".join(_character(_uint32(item) if item > 0 else 0) for item in values)
        return "".join(self.chr_text(item, depth + 1) for item in value)

    def chr_call(self, opening: int) -> str | None:
        """The text a chr() call spells (src/func.cc:489-497), or None when an argument isn't a number, a vector or a range."""
        if opening not in self.spelled:
            pieces: list[str] | None = []
            for start, end in _argument_spans(self.tokens, opening, self.closing):
                value = self.literal(start + 2 if _named(self.tokens, start, end) else start, end, 1)
                if value is None:
                    pieces = None
                    break
                pieces.append(self.chr_text(value, 1))
            self.spelled[opening] = None if pieces is None else "".join(pieces)
        return self.spelled[opening]

    def str_call(self, opening: int, depth: int) -> str:
        """The text a str() call joins (src/func.cc:479-487): its arguments with nothing between them."""
        if depth > TEXT_DEPTH:
            raise ValueError(TOO_DEEP)
        if opening in self.joining:  # a call whose text leads back to itself
            return " "
        if opening not in self.joined:
            self.joining.add(opening)
            pieces = []
            for start, end in _argument_spans(self.tokens, opening, self.closing):
                piece = self.argument_text(start + 2 if _named(self.tokens, start, end) else start, end, depth)
                piece = " " if piece is None else piece
                self.spend(len(piece))
                pieces.append(piece)
            self.joining.discard(opening)
            self.joined[opening] = "".join(pieces)
        return self.joined[opening]

    def argument_text(self, start: int, end: int, depth: int) -> str | None:
        """What an argument gives str(): a string decoded, a number as written, a name's string, or a str() or chr() call worked out.
        None for anything else, which joins as a space."""
        if end - start == 1 and self.tokens[start][0] == "name":
            return self.resolved(self.tokens[start][1], depth + 1)
        if _number(self.tokens, start, end) is not None:
            return "".join(text for _, text in self.tokens[start:end])
        return self.whole_text(start, end, depth)

    def whole_text(self, start: int, end: int, depth: int) -> str | None:
        """The text of the string, str() call or chr() call that makes up tokens[start:end], and None for anything else."""
        if end - start == 1 and self.tokens[start][0] == "string":
            return _decoded(self.tokens[start][1])
        if end - start > 2 and self.tokens[start] in (("name", "str"), ("name", "chr")):
            opening = _call_opening(self.tokens, start)
            if opening is not None and self.closing.get(opening) == end - 1:
                return self.chr_call(opening) if self.tokens[start][1] == "chr" else self.str_call(opening, depth + 1)
        return None

    def resolved(self, name: str, depth: int) -> str | None:
        """The text a name holds when the code gives it a value once, by a top-level assignment of a string, a str() or a chr(), and
        None otherwise, as for a name that leads back to itself."""
        if depth > TEXT_DEPTH:
            raise ValueError(TOO_DEEP)
        if name in self.resolving:
            return None
        if name not in self.names:
            places = self.assigned.get(name, [])
            value = None
            if len(places) == 1 and self.depths[places[0]] == 0 and (places[0] == 0 or self.tokens[places[0] - 1] in _ENDS):
                self.resolving.add(name)
                start = places[0] + 2
                value = self.whole_text(start, self.statement_end(start), depth)
                self.resolving.discard(name)
            self.names[name] = value
        return self.names[name]

    def statement_end(self, start: int) -> int:
        """Where the statement running on from tokens[start] ends: at its semicolon outside brackets, or at the end of the code."""
        at = start
        while at < len(self.tokens) and self.tokens[at] != ("mark", ";"):
            at = self.closing.get(at, len(self.tokens)) + 1 if self.tokens[at] in _OPENING else at + 1
        return min(at, len(self.tokens))


def _decoded(raw: str) -> str:
    """A string token's text as the lexer builds it (src/lexer.l:171-180)."""
    text: list[str] = []
    at, end = 1, len(raw)
    while at < end and raw[at] != '"':
        if raw[at] != "\\":
            if raw[at] != "\n":  # a line break inside a string is left out, and a carriage return stays (lexer.l:179-180)
                text.append(raw[at])
            at += 1
        elif raw[at + 1:at + 2] in _ESCAPED:
            text.append(_ESCAPED[raw[at + 1]])
            at += 2
        elif byte := _HEX_BYTE.match(raw, at):
            value = int(byte.group(1), 16)
            text.append(chr(value) if value else " ")
            at = byte.end()
        elif point := _CODE_POINT.match(raw, at):
            text.append(_character(int(point.group(1) or point.group(2), 16)) or " ")
            at = point.end()
        else:  # no rule matches the backslash, so flex's default rule drops it and the lexer reads on from the next character
            at += 1
    return "".join(text)


def drawn_texts(scad: str) -> list[str]:
    """Text the code can draw that its source doesn't show as written, for the word screen: each string whose escapes or line breaks
    the lexer changes, as it changes them, the text each chr() call spells, and the text each str() call joins. Raises ValueError with
    words for the model when chr() gets anything but numbers, vectors and ranges, or when the text runs past TEXT_BUDGET or
    TEXT_DEPTH."""
    tokens = list(_tokens(scad))
    built = _Built(tokens)
    texts: dict[str, None] = {}
    for at, (kind, raw) in enumerate(tokens):
        if kind == "string":
            decoded = _decoded(raw)
            if decoded != raw[1:-1]:
                texts.setdefault(decoded)
        elif (kind, raw) == ("name", "chr") and (opening := _call_opening(tokens, at)) is not None:
            spelled = built.chr_call(opening)
            if spelled is None:
                raise ValueError(NO_CHR)
            if spelled:
                texts.setdefault(spelled)
        elif (kind, raw) == ("name", "str") and (opening := _call_opening(tokens, at)) is not None:
            joined = built.str_call(opening, 1)
            if joined.strip():
                texts.setdefault(joined)
    return list(texts)


def render(scad: str) -> dict:
    """OpenSCAD source as a 3MF file and a preview PNG. Raises ValueError with words for the model when the code isn't allowed or
    doesn't render."""
    if len(scad) > SCAD_MAX_CHARS:
        raise ValueError(f"The code is over {SCAD_MAX_CHARS} characters. Simplify it.")
    refusal = file_refusal(scad)
    if refusal:
        raise ValueError(refusal)
    texts = drawn_texts(scad)
    folder = tempfile.mkdtemp(prefix="make_model-")
    try:
        with open(os.path.join(folder, "model.scad"), "w", encoding="utf-8") as handle:
            handle.write(scad)
        account = _sandbox_account()
        if account is not None:
            for path in (folder, os.path.join(folder, "model.scad")):
                os.chown(path, *account)
        # The member's code is evaluated once, into a mesh, and the 3MF and the preview are both made from that mesh, so the picture
        # shows what's in the file. OpenSCAD's quick preview draws the CSG instead, and it showed a solid brace as a hole.
        try:
            result = run_held(["openscad", "-o", "model.stl", "model.scad"], folder, RENDER_SECONDS)
        except subprocess.TimeoutExpired:
            raise ValueError(f"The render took over {RENDER_SECONDS} seconds. Lower $fn or avoid minkowski on detailed shapes.") from None
        lines = (result.stdout + result.stderr).splitlines()
        mesh_path = os.path.join(folder, "model.stl")
        if result.returncode != 0 or not os.path.exists(mesh_path) or os.path.getsize(mesh_path) == 0:
            errors = [line for line in lines if "ERROR" in line] or lines
            raise ValueError("OpenSCAD failed:\n" + "\n".join(errors[-12:]))
        with open(os.path.join(folder, "mesh.scad"), "w", encoding="utf-8") as handle:
            handle.write('import("model.stl");\n')
        if account is not None:
            os.chown(os.path.join(folder, "mesh.scad"), *account)
        try:
            packed = run_held(["openscad", "-o", "model.3mf", "mesh.scad"], folder, RENDER_SECONDS)
        except subprocess.TimeoutExpired:
            raise ValueError("Packing the model took too long. Lower $fn or simplify the shape.") from None
        model_path = os.path.join(folder, "model.3mf")
        if packed.returncode != 0 or not os.path.exists(model_path):
            raise ValueError("OpenSCAD couldn't write the 3MF file:\n" + "\n".join((packed.stdout + packed.stderr).splitlines()[-6:]))
        with open(model_path, "rb") as handle:
            model = handle.read()
        if len(model) > MODEL_FILE_MAX_BYTES:
            raise ValueError("The model file is too large to post. Lower $fn or simplify the shape.")
        lows, highs, triangles = mesh_stats(model)
        warnings = [line.strip() for line in lines if "WARNING" in line][:6]

        preview = None
        try:
            shot = run_held(["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24", "openscad", "-o", "preview.png", "--render", "--imgsize=1200,900",
                             "--viewall", "--autocenter", "--colorscheme=Tomorrow", "mesh.scad"], folder, PREVIEW_SECONDS)
            preview_path = os.path.join(folder, "preview.png")
            if shot.returncode == 0 and os.path.exists(preview_path):
                with open(preview_path, "rb") as handle:
                    preview = handle.read()
        except subprocess.TimeoutExpired:
            pass  # the model still goes out without a preview
        return {"model": model, "preview": preview, "lows": lows, "highs": highs, "triangles": triangles, "warnings": warnings,
                "texts": texts}
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def file_name(name: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:40]
    return slug or "model"


async def render_model(args: dict, answer: Answer) -> tuple[str, bool]:
    scad = str(args.get("scad", ""))
    try:
        out = await asyncio.to_thread(render, scad)
    except ValueError as error:
        return str(error), True
    except FileNotFoundError:
        return "Model rendering isn't available on this bot. Tell the member it's unavailable right now.", True
    lows, highs = out["lows"], out["highs"]
    size = f"{highs[0] - lows[0]:.1f} x {highs[1] - lows[1]:.1f} x {highs[2] - lows[2]:.1f} mm"
    name = file_name(args.get("name", "model"))
    answer.files = [(f"{name}.3mf", out["model"]), (f"{name}.scad", scad.encode("utf-8"))]
    answer.image = out["preview"]
    answer.image_name = "preview.png"
    answer.image_label = "Model"
    answer.image_caption = f"{name}.3mf, {size}"
    # The whole source goes to the word screen: embossed text, and also variable names, since one test model carried a blocked word in
    # a variable's name and none in its strings.
    # Beside it goes the text OpenSCAD draws that the source doesn't show as written.
    answer.screen_text = [scad, *out["texts"]]
    report = f"Rendered. Size {size} (X by Y by Z). Lowest point z = {lows[2]:.2f} mm. {out['triangles']} triangles."
    if out["warnings"]:
        report += " OpenSCAD warnings: " + " | ".join(out["warnings"])
    return report, False
