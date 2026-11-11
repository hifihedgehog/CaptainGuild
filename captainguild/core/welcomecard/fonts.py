"""The fonts a welcome picture can use, and the ones that stand in for characters a font lacks.

Twelve families ship in assets/fonts, each with its license beside it, plus DejaVu Sans, which the other cards already use. A family with a
weight axis gives every weight in its range from one file. For a character the chosen family lacks, the search goes on in a fixed order: DejaVu
Sans, then the fonts the machine has, such as Noto Sans CJK on a Linux server. Which font holds a character is read from the font file's own
character table, so nothing is drawn to find out.
"""
from __future__ import annotations

import bisect
import functools
import logging
import os
import struct
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont, features

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
RAQM = features.check("raqm")  # shaping for Arabic and Indic scripts, and right-to-left runs
MAX_HOST_FONTS = 120
BOLD_FROM = 600  # at this weight and up, a stand-in font uses its bold file


@dataclass(frozen=True)
class Family:
    key: str
    name: str
    style: str                      # what kind of face it is, for the picker
    files: tuple[tuple[int, str], ...]  # (weight, file) for each file. A file with a weight axis has weight 0.
    low: int = 400                  # the lightest and heaviest weights the family draws
    high: int = 400
    display: int = 400              # the weight the ready-made layouts use

    @property
    def variable(self) -> bool:
        return self.files[0][0] == 0

    @property
    def weights(self) -> tuple[int, ...]:
        if self.variable:
            return tuple(range(self.low, self.high + 1, 100))
        return tuple(weight for weight, _ in self.files)

    def nearest(self, weight: int) -> int:
        """The weight this family draws for the one asked for."""
        return min(self.weights, key=lambda have: (abs(have - weight), have))

    def heavier(self, weight: int) -> int | None:
        """The weight that reads as bold beside this one, or None when the family has nothing heavier."""
        base = self.nearest(weight)
        heavier = [have for have in self.weights if have > base]
        return min(heavier, key=lambda have: abs(have - (base + 300))) if heavier else None

    def file_for(self, weight: int) -> tuple[str, int]:
        """The file and the axis weight to load. The axis weight is 0 for a file with one fixed weight."""
        if self.variable:
            return self.files[0][1], self.nearest(weight)
        return dict(self.files)[self.nearest(weight)], 0


FAMILIES: dict[str, Family] = {family.key: family for family in (
    Family("fredoka", "Fredoka", "Rounded", ((0, "Fredoka.ttf"),), 300, 700, 600),
    Family("manrope", "Manrope", "Clean sans", ((0, "Manrope.ttf"),), 200, 800, 700),
    Family("lora", "Lora", "Serif", ((0, "Lora.ttf"),), 400, 700, 600),
    Family("robotoslab", "Roboto Slab", "Slab", ((0, "RobotoSlab.ttf"),), 100, 900, 700),
    Family("playfair", "Playfair Display", "Elegant serif", ((0, "PlayfairDisplay.ttf"),), 400, 900, 700),
    Family("anton", "Anton", "Poster", ((400, "Anton.ttf"),)),
    Family("bangers", "Bangers", "Comic", ((400, "Bangers.ttf"),)),
    Family("pacifico", "Pacifico", "Brush script", ((400, "Pacifico.ttf"),)),
    Family("caveat", "Caveat", "Handwriting", ((0, "Caveat.ttf"),), 400, 700, 700),
    Family("orbitron", "Orbitron", "Sci-fi", ((0, "Orbitron.ttf"),), 400, 900, 700),
    Family("pressstart", "Press Start 2P", "Pixel", ((400, "PressStart2P.ttf"),)),
    Family("jetbrainsmono", "JetBrains Mono", "Monospace", ((0, "JetBrainsMono.ttf"),), 100, 800, 700),
    Family("dejavu", "DejaVu Sans", "Plain, most scripts", ((400, "DejaVuSans.ttf"), (700, "DejaVuSans-Bold.ttf")), 400, 700, 700),
)}
KEYS: tuple[str, ...] = tuple(FAMILIES)
DEFAULT = "fredoka"

# Where a machine keeps the fonts that cover the scripts DejaVu lacks. Windows and macOS have fixed names. Linux differs by distribution,
# so its folders are walked once and the Noto and Droid files found there are used, in the order `_host_rank` gives them.
_WINDOWS = ("segoeui.ttf", "msyh.ttc", "malgun.ttf", "YuGothR.ttc", "msgothic.ttc", "msjh.ttc", "simsun.ttc", "Nirmala.ttc", "Nirmala.ttf", "leelawui.ttf", "tahoma.ttf",
            "arial.ttf", "ebrima.ttf", "gadugi.ttf", "mvboli.ttf", "seguisym.ttf", "seguihis.ttf", "sylfaen.ttf", "himalaya.ttf")
_MAC = ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf", "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc")
_LINUX_FOLDERS = ("/usr/share/fonts", "/usr/local/share/fonts")


def _host_rank(name: str) -> tuple[int, str] | None:
    """Where a font file on a Linux machine comes in the search, or None when it isn't one this uses."""
    lowered = name.lower()
    if not lowered.endswith((".ttf", ".otf", ".ttc")) or "italic" in lowered or "emoji" in lowered or "mono" in lowered:
        return None
    stem = lowered.rsplit(".", 1)[0]
    if stem == "notosans-regular":
        return 0, lowered
    if stem.startswith("notosanscjk") and stem.endswith("regular"):
        return 1, lowered
    if stem.startswith("droidsansfallback"):
        return 2, lowered
    if stem.startswith(("notosans", "notonaskh", "notokufi")) and stem.endswith("regular") and "display" not in stem and "ui-" not in stem:
        return 3, lowered
    return None


@functools.lru_cache(maxsize=1)
def host_fonts() -> tuple[str, ...]:
    """The machine's own fonts that can stand in for a character, in the order they're tried. Read once."""
    found: list[str] = []
    if sys.platform == "win32":
        folder = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        found = [str(folder / name) for name in _WINDOWS if (folder / name).is_file()]
    elif sys.platform == "darwin":
        found = [path for path in _MAC if os.path.isfile(path)]
    else:
        ranked: list[tuple[tuple[int, str], str]] = []
        for root in _LINUX_FOLDERS:
            for folder, _, names in os.walk(root):
                for name in names:
                    rank = _host_rank(name)
                    if rank is not None:
                        ranked.append((rank, os.path.join(folder, name)))
                if len(ranked) > 4 * MAX_HOST_FONTS:
                    break
        seen: set[str] = set()
        for rank, path in sorted(ranked):
            if rank[1] not in seen:  # the same file installed in two folders counts once
                seen.add(rank[1])
                found.append(path)
    return tuple(found[:MAX_HOST_FONTS])


class Coverage:
    """The characters a font file holds, as sorted ranges."""

    def __init__(self, ranges: list[tuple[int, int]]) -> None:
        merged: list[list[int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self.starts = [start for start, _ in merged]
        self.ends = [end for _, end in merged]

    def __contains__(self, code: int) -> bool:
        at = bisect.bisect_right(self.starts, code) - 1
        return at >= 0 and code <= self.ends[at]

    def __len__(self) -> int:
        return sum(end - start + 1 for start, end in zip(self.starts, self.ends))


def _table(data: bytes, base: int, tag: bytes) -> int | None:
    count = struct.unpack_from(">H", data, base + 4)[0]
    for index in range(count):
        entry = base + 12 + index * 16
        if data[entry:entry + 4] == tag:
            return struct.unpack_from(">I", data, entry + 8)[0]
    return None


def _format_4(data: bytes, at: int) -> list[tuple[int, int]]:
    segments = struct.unpack_from(">H", data, at + 6)[0] // 2
    ends = struct.unpack_from(f">{segments}H", data, at + 14)
    starts = struct.unpack_from(f">{segments}H", data, at + 16 + 2 * segments)
    deltas = struct.unpack_from(f">{segments}h", data, at + 16 + 4 * segments)
    offsets_at = at + 16 + 6 * segments
    offsets = struct.unpack_from(f">{segments}H", data, offsets_at)
    ranges: list[tuple[int, int]] = []
    for index in range(segments):
        start, end = starts[index], ends[index]
        if start > end or start == 0xFFFF:
            continue
        if offsets[index] == 0:
            missing = (-deltas[index]) & 0xFFFF  # the one code in the segment, if any, that lands on glyph 0
            if start <= missing <= end:
                if start < missing:
                    ranges.append((start, missing - 1))
                if missing < end:
                    ranges.append((missing + 1, end))
            else:
                ranges.append((start, end))
            continue
        run_start = None
        for code in range(start, end + 1):
            glyph_at = offsets_at + 2 * index + offsets[index] + 2 * (code - start)
            glyph = struct.unpack_from(">H", data, glyph_at)[0] if glyph_at + 2 <= len(data) else 0
            if glyph and run_start is None:
                run_start = code
            elif not glyph and run_start is not None:
                ranges.append((run_start, code - 1))
                run_start = None
        if run_start is not None:
            ranges.append((run_start, end))
    return ranges


def _format_12(data: bytes, at: int) -> list[tuple[int, int]]:
    groups = struct.unpack_from(">I", data, at + 12)[0]
    ranges = []
    for index in range(min(groups, 200_000)):
        start, end, glyph = struct.unpack_from(">III", data, at + 16 + 12 * index)
        if start > end or end > 0x10FFFF:
            continue
        if glyph == 0:  # the group's first code is the missing glyph
            start += 1
        if start <= end:
            ranges.append((start, end))
    return ranges


@functools.lru_cache(maxsize=256)
def coverage(path: str, index: int = 0) -> Coverage | None:
    """Which characters a font file holds, from its character table. None when the file can't be read that way."""
    try:
        with open(path, "rb") as source:
            data = source.read()
        base = 0
        if data[:4] == b"ttcf":
            count = struct.unpack_from(">I", data, 8)[0]
            base = struct.unpack_from(">I", data, 12 + 4 * min(index, count - 1))[0]
        cmap = _table(data, base, b"cmap")
        if cmap is None:
            return None
        tables = struct.unpack_from(">H", data, cmap + 2)[0]
        best: tuple[int, int] | None = None
        for number in range(tables):
            platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + 8 * number)
            kind = struct.unpack_from(">H", data, cmap + offset)[0]
            unicode_table = platform == 0 or (platform == 3 and encoding in (1, 10))
            if unicode_table and kind in (4, 12) and (best is None or kind > best[0]):
                best = (kind, cmap + offset)
        if best is None:
            return None
        return Coverage(_format_12(data, best[1]) if best[0] == 12 else _format_4(data, best[1]))
    except (OSError, struct.error, IndexError, ValueError) as error:
        log.debug("Could not read the character table of %s: %s", path, error)
        return None


@functools.lru_cache(maxsize=512)
def _load(path: str, weight: int, size: int, shaped: bool = True) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(path, size, layout_engine=None if shaped else ImageFont.Layout.BASIC)
    if weight:
        try:
            axes = face.get_variation_axes()
            values = []
            for axis in axes:
                name = axis["name"].decode("ascii", "replace") if isinstance(axis["name"], bytes) else str(axis["name"])
                values.append(max(axis["minimum"], min(axis["maximum"], weight)) if name == "Weight" else axis["default"])
            face.set_variation_by_axes(values)
        except (OSError, AttributeError, KeyError) as error:  # a build of FreeType without weight axes draws the file's own weight
            log.debug("Could not set the weight of %s: %s", path, error)
    return face


@dataclass(frozen=True)
class Source:
    """One font file at one weight: the chosen family, or a stand-in for the characters it lacks."""
    path: str
    weight: int = 0        # the weight axis value, or 0 for a file with one fixed weight
    thicken: bool = False  # the run asked for bold and this file has nothing heavier, so its letters are drawn thicker

    def at(self, size: int, shaped: bool = True) -> ImageFont.FreeTypeFont:
        """The font at this size. Unshaped, it sets letters one after another, for text the shaper refuses."""
        return _load(self.path, self.weight, max(1, int(size)), shaped)

    def covers(self, character: str) -> bool:
        table = coverage(self.path)
        if table is not None:
            return ord(character) in table
        return _draws(self.path, character)


@functools.lru_cache(maxsize=8192)
def _draws(path: str, character: str) -> bool:
    """For a file whose character table can't be read: whether drawing the character gives anything but the missing-character box."""
    try:
        face = _load(path, 0, 20)
        mask = face.getmask(character)
        return bool(mask.size[0]) and bytes(mask) != bytes(face.getmask("\U0010FFFF"))
    except (OSError, ValueError):
        return False


@functools.lru_cache(maxsize=256)
def chain(family: str, weight: int, bold: bool = False) -> tuple[Source, ...]:
    """The fonts to try for each character, in order: the family at this weight, DejaVu Sans, then the machine's own fonts."""
    chosen = FAMILIES.get(family) or FAMILIES[DEFAULT]
    wanted = chosen.heavier(weight) if bold else chosen.nearest(weight)
    # A step of one weight is too small to see, so the letters are also drawn a little thicker, as they are when nothing heavier exists.
    thicken = bold and (wanted is None or wanted - chosen.nearest(weight) < 200)
    file, axis = chosen.file_for(wanted if wanted is not None else weight)
    sources = [Source(str(FONT_DIR / file), axis, thicken)]
    heavy = bold or chosen.nearest(weight) >= BOLD_FROM
    if chosen.key != "dejavu":
        sources.append(Source(str(FONT_DIR / ("DejaVuSans-Bold.ttf" if heavy else "DejaVuSans.ttf"))))
    sources.extend(Source(path, 0, bold) for path in host_fonts())
    return tuple(sources)


def pick(sources: tuple[Source, ...], character: str) -> int | None:
    """Which font in the chain draws this character, or None when none does."""
    for index, source in enumerate(sources):
        if source.covers(character):
            return index
    return None


def simpler(character: str) -> str:
    """The plain form of a styled character no font here holds, such as the script and double-struck letters people put in names. Empty when it has none."""
    plain = unicodedata.normalize("NFKC", character)
    return plain if plain != character else ""


def catalog() -> list[dict]:
    """The families for the page's picker."""
    return [{"key": family.key, "name": family.name, "style": family.style, "weights": list(family.weights), "display": family.display} for family in FAMILIES.values()]
