"""The drawing pieces statistics cards share: fonts, text that fits its room, panels, pictures and charts.

A card is drawn at twice its size and scaled down at the end, which gives every edge, line and letter a smooth outline. Coordinates passed
in are the finished card's pixels. Nothing here touches Discord or the database, so cards.py runs all of it on a worker thread.

Colors come from the dashboard's dark theme, so a card looks like the page it summarizes. Chart series use the categorical palette in its
dark steps, in its fixed order, on the darkest of those surfaces, where every step has at least 3:1 contrast. Text never wears a series
color: a swatch beside the text says which series it is.
"""
from __future__ import annotations

import functools
import io
import math
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .. import images

WIDTH = 1280
S = 2  # drawing scale
FONT_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
REGULAR = FONT_DIR / "DejaVuSans.ttf"
BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
# Where the usual systems put a color emoji font: Noto Color Emoji on Linux (AlmaLinux's google-noto-emoji-color-fonts package takes the
# first path, Debian and Ubuntu the third) and Segoe UI Emoji on Windows. Without one, an emoji shows as the plain font's outline of it, or by
# its name.
EMOJI_FONTS = (
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf", "/usr/share/fonts/noto-emoji/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", "/usr/share/fonts/noto/NotoColorEmoji.ttf", "C:/Windows/Fonts/seguiemj.ttf",
)

# The dashboard's dark theme (web/static/css/app.css)
BG = (47, 53, 58)          # --bg, the card
PANEL = (58, 65, 73)       # --panel
WELL = (35, 40, 44)        # --line, the chart surface and the rows inside panels
TEXT = (228, 231, 234)     # --text
SOFT = (182, 191, 199)     # --text-soft
DIM = (141, 152, 162)      # --text-dim
GRID = (47, 53, 58)        # one step off the chart surface
AXIS = (81, 91, 101)       # --input, the baseline
ACCENT = (0, 162, 248)     # --accent
# The categorical palette, dark steps, in its fixed order. Checked with the palette validator against WELL: every check passes.
SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
OTHER = "#898781"          # the folded rest, the palette's muted gray
UP, DOWN = "#3987e5", "#e66767"  # the palette's diverging pair: joins above the line, leaves below

LINE = 4.5  # a chart line, about 2 pixels where Discord shows the card
DOT = 9    # an end marker's radius
GAP = 4    # the surface gap between touching marks
CORNER = 9  # a bar's rounded end
WASH = 28  # an area's fill opacity out of 255, about 11 percent


def rgb(color: str | tuple) -> tuple:
    """A "#rrggbb" color as a tuple. A tuple passes through as it is, opacity and all."""
    if isinstance(color, tuple):
        return color
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


@functools.lru_cache(maxsize=128)
def font(size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(BOLD if bold else REGULAR), max(4, round(size * S)))


@functools.lru_cache(maxsize=1)
def _emoji_font_path() -> str | None:
    for path in EMOJI_FONTS:
        if path and os.path.exists(path):
            try:
                ImageFont.truetype(path, 109)
                return path
            except OSError:
                continue
    return None


@functools.lru_cache(maxsize=8)
def _emoji_font() -> ImageFont.FreeTypeFont | None:
    path = _emoji_font_path()
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, 109)  # the size a bitmap color font is made at, which a vector one takes as well
    except OSError:
        return None


@functools.lru_cache(maxsize=8192)
def can_draw(character: str, bold: bool = False) -> bool:
    """Whether the font has this character, rather than drawing an empty box for it."""
    if character in " \u00a0\u202f":
        return True
    face = font(20, bold)
    mask = face.getmask(character)
    return bool(mask.size[0]) and bytes(mask) != bytes(face.getmask("\U0010FFFF"))


_JOINERS = frozenset("\u200d\ufe0f\u20e3")  # zero-width joiner, emoji presentation, keycap
EMOJI_HEIGHT = 1.08  # an emoji's height beside text, in multiples of the text's size
EMOJI_SPACE = 0.08  # the room on each side of an emoji, in multiples of the text's size


def _joins(character: str) -> bool:
    code = ord(character)
    return character in _JOINERS or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F  # skin tones and the tags of subdivision flags


def _regional(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def clusters(text: str) -> list[str]:
    """The text cut where a reader sees one character end. An emoji keeps its skin tone, its joined parts, its keycap and its presentation
    selector, and two regional letters make one flag."""
    out: list[str] = []
    for character in text:
        if out and (_joins(character) or out[-1].endswith("\u200d")):
            out[-1] += character
        elif out and _regional(character) and len(out[-1]) == 1 and _regional(out[-1]):
            out[-1] += character
        else:
            out.append(character)
    return out


def _emoji_like(cluster: str, bold: bool = False) -> bool:
    """Whether the cluster is one the color emoji font should draw: one joined or marked for emoji, a flag, or one the text font lacks."""
    return (len(cluster) > 1 and (any(_joins(character) for character in cluster) or _regional(cluster[0]))) or not can_draw(cluster[0], bold)


@functools.lru_cache(maxsize=4096)
def emoji_picture(cluster: str) -> Image.Image | None:
    """The cluster in color, cut to its ink, from the color emoji font. None without the font, or when the font has nothing for it."""
    face = _emoji_font()
    if face is None:
        return None
    layer = Image.new("RGBA", (420, 180), (0, 0, 0, 0))
    try:
        ImageDraw.Draw(layer).text((210, 90), cluster, font=face, embedded_color=True, anchor="mm")
    except (OSError, ValueError):
        return None
    box = layer.getbbox()
    if box is None:
        return None
    picture = layer.crop(box)
    if not any(r != g or g != b for r, g, b, a in picture.getdata() if a > 128):
        return None  # only gray ink: the font's empty box, or no emoji at all
    return picture


def runs(text: str, bold: bool = False) -> list[tuple[str, str]]:
    """The text as ("text", letters) runs for the text font and ("emoji", cluster) for each emoji the color font draws."""
    out: list[tuple[str, str]] = []
    for cluster in clusters(text):
        if _emoji_like(cluster, bold) and emoji_picture(cluster) is not None:
            out.append(("emoji", cluster))
        elif out and out[-1][0] == "text":
            out[-1] = ("text", out[-1][1] + cluster)
        else:
            out.append(("text", cluster))
    return out


def emoji_advance(cluster: str, size: float) -> float:
    """How much room an emoji takes beside text of this size."""
    picture = emoji_picture(cluster)
    if picture is None:
        return 0.0
    return size * EMOJI_HEIGHT * picture.width / picture.height + 2 * size * EMOJI_SPACE


def drawable(text: str, fallback: str = "", bold: bool = False) -> str:
    """Text without the characters neither font can draw, with spaces tidied: an emoji stays when the machine has a color emoji font that
    draws it. The fallback when nothing is left."""
    kept = []
    for cluster in clusters(text or ""):
        if _emoji_like(cluster, bold):
            if emoji_picture(cluster) is not None:
                kept.append(cluster)
            else:
                kept.append("".join(character for character in cluster if not _joins(character) and can_draw(character, bold)))
        else:
            kept.append(cluster)
    shown = " ".join("".join(kept).split())
    return shown or fallback


def readable(text: str | None, bold: bool = False) -> str | None:
    """A name as the font can draw it, or None when it can't draw one of its letters or digits. Emoji and symbols it lacks just drop out,
    so Sam with a game controller after it reads as Sam, but a Japanese name followed by 3D isn't cut down to 3D."""
    if not text:
        return None
    if any(character.isalnum() and not can_draw(character, bold) for character in text):
        return None
    return drawable(text, "", bold) or None


def _open(data: bytes) -> Image.Image | None:
    """A picture from Discord: an icon, avatar, emoji or sticker. Its format and size are checked before it's decoded."""
    try:
        with Image.open(io.BytesIO(data), formats=images.OPENED_FORMATS) as image:
            if max(image.size) > images.MAX_SIDE:
                return None
            image.seek(0)
            return image.convert("RGBA")
    except images.READ_ERRORS:
        return None
    except Exception:  # an unreadable picture leaves the card without it
        return None


class Card:
    """One card being drawn."""

    def __init__(self, height: int, width: int = WIDTH) -> None:
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width * S, height * S), BG)
        self.draw = ImageDraw.Draw(self.image, "RGBA")

    # ---- shapes ----

    def rect(self, box: Sequence[float], fill: Any, radius: float = 0) -> None:
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            return
        scaled = (x0 * S, y0 * S, x1 * S - 1, y1 * S - 1)
        color = rgb(fill) if isinstance(fill, str) else fill
        if radius:
            self.draw.rounded_rectangle(scaled, radius=min(radius * S, (x1 - x0) * S / 2, (y1 - y0) * S / 2), fill=color)
        else:
            self.draw.rectangle(scaled, fill=color)

    def bar(self, box: Sequence[float], fill: Any, radius: float = CORNER, *, up: bool = True) -> None:
        """A bar rounded at its data end and square where it meets its baseline."""
        x0, y0, x1, y1 = box
        if x1 - x0 < 0.5 or y1 - y0 < 0.5:
            return
        corner = min(radius, (x1 - x0) / 2, y1 - y0)
        self.rect(box, fill, corner)
        if corner and y1 - y0 > corner:
            square = (x0, y1 - corner, x1, y1) if up else (x0, y0, x1, y0 + corner)
            self.rect(square, fill)

    def line(self, points: Sequence[tuple[float, float]], color: Any, width: float = LINE) -> None:
        if len(points) < 2:
            return
        self.draw.line([(x * S, y * S) for x, y in points], fill=rgb(color) if isinstance(color, str) else color, width=max(1, round(width * S)), joint="curve")
        radius = width * S / 2
        for x, y in (points[0], points[-1]):  # round caps
            self.draw.ellipse((x * S - radius, y * S - radius, x * S + radius, y * S + radius), fill=rgb(color) if isinstance(color, str) else color)

    def hairline(self, x0: float, y0: float, x1: float, y1: float, color: Any = GRID, width: float = 1) -> None:
        self.draw.line([(x0 * S, y0 * S), (x1 * S, y1 * S)], fill=color, width=max(1, round(width * S)))

    def polygon(self, points: Sequence[tuple[float, float]], fill: Any) -> None:
        if len(points) >= 3:
            self.draw.polygon([(x * S, y * S) for x, y in points], fill=fill)

    def dot(self, x: float, y: float, color: Any, radius: float = DOT, ring: Any = WELL) -> None:
        """An end marker with a ring of the surface around it."""
        outer = (radius + GAP / 2) * S
        self.draw.ellipse((x * S - outer, y * S - outer, x * S + outer, y * S + outer), fill=ring)
        self.draw.ellipse((x * S - radius * S, y * S - radius * S, x * S + radius * S, y * S + radius * S), fill=rgb(color) if isinstance(color, str) else color)

    def swatch(self, x: float, y: float, color: Any, size: float = 16) -> None:
        """A legend's key: a small rounded square, centered on (x, y)."""
        self.rect((x - size / 2, y - size / 2, x + size / 2, y + size / 2), color, 4)

    # ---- text ----

    def measure(self, text: str, size: float, bold: bool = False) -> float:
        if not text:
            return 0.0
        face = font(size, bold)
        return sum(self.draw.textlength(value, font=face) / S if kind == "text" else emoji_advance(value, size) for kind, value in runs(text, bold))

    def text(self, x: float, y: float, text: str, size: float, *, bold: bool = False, color: Any = TEXT, anchor: str = "ls") -> float:
        """Draws text and returns its width. `anchor` is Pillow's: "ls" left on the baseline, "rs" right, "ms" centered, "lm" left middle."""
        if not text:
            return 0.0
        parts = runs(text, bold)
        if all(kind == "text" for kind, _ in parts):
            self.draw.text((x * S, y * S), text, font=font(size, bold), fill=color, anchor=anchor)
            return self.measure(text, size, bold)
        # Letters and emoji side by side: the letters in the text font on one baseline, each emoji in color beside them.
        face = font(size, bold)
        width = self.measure(text, size, bold)
        across, down = anchor[0], anchor[1]
        cursor = x - (width if across == "r" else width / 2 if across == "m" else 0)
        ascent, descent = (value / S for value in face.getmetrics())
        baseline = {"a": y + ascent, "t": y + ascent, "m": y + (ascent - descent) / 2, "b": y - descent, "d": y - descent}.get(down, y)
        for kind, value in parts:
            if kind == "text":
                self.draw.text((cursor * S, baseline * S), value, font=face, fill=color, anchor="ls")
                cursor += self.draw.textlength(value, font=face) / S
                continue
            picture = emoji_picture(value)
            height = size * EMOJI_HEIGHT
            scaled = picture.resize((max(1, round(height * picture.width / picture.height * S)), max(1, round(height * S))), Image.Resampling.LANCZOS)
            middle = baseline - size * 0.36  # where capital letters have their middle
            left = cursor + size * EMOJI_SPACE
            self.image.paste(scaled, (round(left * S), round(middle * S - scaled.height / 2)), scaled)
            cursor += emoji_advance(value, size)
        return width

    def fit(self, text: str, width: float, size: float, *, minimum: float | None = None, bold: bool = False) -> tuple[str, float]:
        """The largest size down to `minimum` that fits, and then the text cut short with an ellipsis if even that doesn't."""
        minimum = minimum or size * 0.7
        current = size
        while current >= minimum:
            if self.measure(text, current, bold) <= width:
                return text, current
            current -= 1
        current = minimum
        parts = clusters(text)  # cut between characters as a reader sees them, never through an emoji
        while parts and self.measure("".join(parts) + "\u2026", current, bold) > width:
            parts.pop()
        text = "".join(parts)
        return (text.rstrip() + "\u2026") if text else "", current

    def fitted(self, x: float, y: float, text: str, width: float, size: float, *, minimum: float | None = None, bold: bool = False, color: Any = TEXT,
               anchor: str = "ls") -> float:
        shown, chosen = self.fit(text, width, size, minimum=minimum, bold=bold)
        return self.text(x, y, shown, chosen, bold=bold, color=color, anchor=anchor)

    def emoji_glyph(self, x: float, y: float, text: str, size: float) -> bool:
        """Draws an emoji centered on (x, y) in color when the machine has a color emoji font, else in the plain font when it has the
        character. Returns whether anything was drawn."""
        face = _emoji_font()
        if face is not None:
            layer = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            try:
                ImageDraw.Draw(layer).text((80, 80), text, font=face, embedded_color=True, anchor="mm")
            except (OSError, ValueError):
                layer = None
            if layer is not None and layer.getbbox():
                side = round(size * S)
                fitted = layer.crop(layer.getbbox())
                fitted.thumbnail((side, side), Image.Resampling.LANCZOS)
                self.image.paste(fitted, (round(x * S - fitted.width / 2), round(y * S - fitted.height / 2)), fitted)
                return True
        plain = drawable(text)
        if plain and plain == text.replace("\ufe0f", ""):
            self.text(x, y, plain, size * 0.8, anchor="mm")
            return True
        return False

    # ---- pictures ----

    def picture(self, data: bytes | None, box: Sequence[float], *, round_: bool = True, letter: str = "", corner: float = 0) -> None:
        """A picture filling the box, cut to a circle or a rounded square. Without one, the box shows a letter."""
        x0, y0, x1, y1 = box
        side_w, side_h = round((x1 - x0) * S), round((y1 - y0) * S)
        image = _open(data) if data else None
        if image is None:
            if round_:
                self.draw.ellipse((x0 * S, y0 * S, x1 * S - 1, y1 * S - 1), fill=PANEL if round_ else WELL)
            else:
                self.rect(box, PANEL, corner)
            if letter:
                self.text((x0 + x1) / 2, (y0 + y1) / 2, letter[:1].upper(), (y1 - y0) * 0.45, bold=True, color=SOFT, anchor="mm")
            return
        fitted = ImageOps.fit(image, (side_w, side_h), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (side_w, side_h), 0)
        if round_:
            ImageDraw.Draw(mask).ellipse((0, 0, side_w - 1, side_h - 1), fill=255)
        else:
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, side_w - 1, side_h - 1), radius=corner * S, fill=255)
        alpha = Image.composite(fitted.getchannel("A"), Image.new("L", (side_w, side_h), 0), mask)
        self.image.paste(fitted.convert("RGB"), (round(x0 * S), round(y0 * S)), alpha)

    def sticker_or_emoji(self, data: bytes | None, x: float, y: float, size: float) -> bool:
        """A server emoji's or sticker's picture centered on (x, y). Returns whether there was one to draw."""
        image = _open(data) if data else None
        if image is None:
            return False
        side = round(size * S)
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
        self.image.paste(image, (round(x * S - image.width / 2), round(y * S - image.height / 2)), image)
        return True

    # ---- icons ----

    def icon(self, name: str, x: float, y: float, size: float = 28, color: Any = DIM) -> None:
        """A small icon centered on (x, y): hash, speaker, person, people, trophy, chart, star, mail, smile or gamepad."""
        half = size / 2
        unit = size / 24
        if name == "hash":
            for offset in (-3.5, 3.5):
                self.hairline(x + offset * unit - 1.5 * unit, y + 9 * unit, x + offset * unit + 1.5 * unit, y - 9 * unit, color, 2.6 * unit)
            for offset in (-3.5, 3.5):
                self.hairline(x - 8.5 * unit, y + offset * unit, x + 8.5 * unit, y + offset * unit, color, 2.6 * unit)
        elif name == "speaker":
            self.polygon([(x - 9 * unit, y - 3.5 * unit), (x - 4 * unit, y - 3.5 * unit), (x + 2 * unit, y - 9 * unit), (x + 2 * unit, y + 9 * unit),
                          (x - 4 * unit, y + 3.5 * unit), (x - 9 * unit, y + 3.5 * unit)], color)
            for radius in (5.5, 10):
                box = ((x + 1 * unit - radius * unit) * S, (y - radius * unit) * S, (x + 1 * unit + radius * unit) * S, (y + radius * unit) * S)
                self.draw.arc(box, -45, 45, fill=color, width=max(1, round(2.4 * unit * S)))
        elif name in ("person", "people"):
            offsets = (0,) if name == "person" else (-5, 5)
            for offset in offsets:
                cx = x + offset * unit
                self.draw.ellipse(((cx - 4.5 * unit) * S, (y - 10 * unit) * S, (cx + 4.5 * unit) * S, (y - 1 * unit) * S), fill=color)
                self.draw.pieslice(((cx - 8 * unit) * S, (y + 1 * unit) * S, (cx + 8 * unit) * S, (y + 17 * unit) * S), 180, 360, fill=color)
        elif name == "trophy":
            self.draw.pieslice(((x - 7 * unit) * S, (y - 16 * unit) * S, (x + 7 * unit) * S, (y + 2 * unit) * S), 0, 180, fill=color)
            self.rect((x - 7 * unit, y - 9 * unit, x + 7 * unit, y - 7 * unit), color)
            self.rect((x - 1.5 * unit, y + 1 * unit, x + 1.5 * unit, y + 6 * unit), color)
            self.rect((x - 6 * unit, y + 6 * unit, x + 6 * unit, y + 9 * unit), color, 1.5 * unit)
            for side in (-1, 1):
                box = ((x + side * 8 * unit - 3.5 * unit) * S, (y - 8 * unit) * S, (x + side * 8 * unit + 3.5 * unit) * S, (y - 1 * unit) * S)
                self.draw.arc(box, 90 if side < 0 else -90, 270 if side < 0 else 90, fill=color, width=max(1, round(2 * unit * S)))
        elif name == "chart":
            for index, height in enumerate((7, 13, 18)):
                left = x - 9 * unit + index * 6.5 * unit
                self.bar((left, y + 9 * unit - height * unit, left + 4.5 * unit, y + 9 * unit), color, 1.5 * unit)
        elif name == "star":
            points = []
            for index in range(10):
                angle = math.pi / 2 + index * math.pi / 5
                radius = (10 if index % 2 == 0 else 4.2) * unit
                points.append((x + radius * math.cos(angle), y - radius * math.sin(angle)))
            self.polygon(points, color)
        elif name == "mail":
            self.rect((x - 10 * unit, y - 7 * unit, x + 10 * unit, y + 7 * unit), color, 2 * unit)
            self.draw.line([((x - 9 * unit) * S, (y - 6 * unit) * S), (x * S, (y + 1 * unit) * S), ((x + 9 * unit) * S, (y - 6 * unit) * S)], fill=PANEL,
                           width=max(1, round(2 * unit * S)))
        elif name == "smile":
            self.draw.ellipse(((x - 10 * unit) * S, (y - 10 * unit) * S, (x + 10 * unit) * S, (y + 10 * unit) * S), outline=color, width=max(1, round(2.4 * unit * S)))
            for side in (-1, 1):
                self.draw.ellipse(((x + side * 3.5 * unit - 1.6 * unit) * S, (y - 4.5 * unit) * S, (x + side * 3.5 * unit + 1.6 * unit) * S, (y - 1.3 * unit) * S), fill=color)
            self.draw.arc(((x - 5.5 * unit) * S, (y - 4 * unit) * S, (x + 5.5 * unit) * S, (y + 6 * unit) * S), 20, 160, fill=color, width=max(1, round(2.2 * unit * S)))
        elif name == "gamepad":
            self.rect((x - 11 * unit, y - 6 * unit, x + 11 * unit, y + 7 * unit), color, 6 * unit)
            self.rect((x - 7.5 * unit, y - 0.8 * unit, x - 2.5 * unit, y + 1.8 * unit), PANEL)
            self.rect((x - 6.3 * unit, y - 2 * unit, x - 3.7 * unit, y + 3 * unit), PANEL)
            for dx, dy in ((4.5, 0.5), (7.5, -2)):
                self.draw.ellipse(((x + dx * unit - 1.4 * unit) * S, (y + dy * unit - 1.4 * unit) * S, (x + dx * unit + 1.4 * unit) * S, (y + dy * unit + 1.4 * unit) * S), fill=PANEL)
        elif name == "folder":
            self.rect((x - 10 * unit, y - 6 * unit, x + 10 * unit, y + 8 * unit), color, 2 * unit)
            self.rect((x - 10 * unit, y - 9 * unit, x - 2 * unit, y - 4 * unit), color, 2 * unit)
        else:
            self.draw.ellipse(((x - half / 2) * S, (y - half / 2) * S, (x + half / 2) * S, (y + half / 2) * S), fill=color)

    # ---- finishing ----

    def end_at(self, bottom: float, footer_room: float = 62) -> None:
        """Cuts the card just below its last panel, leaving room for the footer."""
        height = int(math.ceil(bottom + footer_room))
        if height < self.height:
            self.image = self.image.crop((0, 0, self.width * S, height * S))
            self.draw = ImageDraw.Draw(self.image, "RGBA")
            self.height = height

    def png(self) -> bytes:
        finished = self.image.resize((self.width, self.height), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        finished.save(out, format="PNG", optimize=False, compress_level=6)
        return out.getvalue()


# ---- panels and layout ----

PAD = 22
TITLE = 26


def panel(card: Card, box: Sequence[float], title: str = "", icon: str = "") -> tuple[float, float, float, float]:
    """A panel with an optional title and icon. Returns the room left inside it."""
    x0, y0, x1, y1 = box
    card.rect(box, PANEL, 18)
    top = y0 + PAD
    if title:
        room = x1 - x0 - 2 * PAD - (36 if icon else 0)
        card.fitted(x0 + PAD, y0 + PAD + TITLE * 0.85, title, room, TITLE, bold=True, minimum=19)
        if icon:
            card.icon(icon, x1 - PAD - 14, y0 + PAD + TITLE * 0.48, 28)
        top = y0 + PAD + TITLE + 16
    return x0 + PAD, top, x1 - PAD, y1 - PAD


def rows_area(card: Card, box: Sequence[float], rows: Sequence[tuple], *, label_width: float = 150, size: float = 30) -> None:
    """Rows of label, value and unit, each on its own band of the chart surface, filling the box's height. A row may carry a short unit
    fourth. Every row shows its unit or none does, so a panel reads the same all the way down: the long unit where all of them fit, else
    the short one where rows have one, else none."""
    x0, y0, x1, y1 = box
    if not rows:
        return
    gap = 10
    height = (y1 - y0 - gap * (len(rows) - 1)) / len(rows)
    label_room = min(label_width, (x1 - x0) * 0.42)
    value_size = min(size, height * 0.62)
    unit_size = value_size * 0.72
    room = x1 - 14 - (x0 + label_room)

    def fits(which: int) -> bool:
        for row in rows:
            unit_text = row[which] if len(row) > which else ""
            if card.measure(row[1], value_size, True) + (card.measure(" " + unit_text, unit_size) if unit_text else 0) > room:
                return False
        return True

    which = 2 if fits(2) else 3 if any(len(row) > 3 and row[3] for row in rows) and fits(3) else None
    for index, row in enumerate(rows):
        label, value = row[0], row[1]
        unit_text = row[which] if which is not None and len(row) > which else ""
        top = y0 + index * (height + gap)
        card.rect((x0, top, x1, top + height), WELL, 10)
        middle = top + height / 2
        card.fitted(x0 + 16, middle, label, label_room - 20, size * 0.8, minimum=size * 0.6, color=SOFT, anchor="lm")
        left = x0 + label_room
        shown, chosen = card.fit(value, room, value_size, bold=True, minimum=value_size * 0.6)
        value_width = card.text(left, middle, shown, chosen, bold=True, anchor="lm")
        if unit_text:
            card.text(left + value_width, middle, " " + unit_text, unit_size, color=SOFT, anchor="lm")


def name_value_rows(card: Card, box: Sequence[float], rows: Sequence[dict], *, size: float = 28, rank: bool = False, swatches: Sequence[Any] = (),
                    marker: Callable[[Card, dict, float, float, float], float] | None = None, empty: str = "") -> None:
    """Rows of a name and a value: a top-three list or a page of a ranking. A row's "name" is drawn as given, "value" right-aligned.
    `marker` draws a picture before the name and returns the room it took."""
    x0, y0, x1, y1 = box
    if not rows:
        if empty:
            card.fitted(x0, (y0 + y1) / 2, empty, x1 - x0, size * 0.8, color=DIM, anchor="lm", minimum=14)
        return
    gap = 10
    count = max(len(rows), 1)
    height = (y1 - y0 - gap * (count - 1)) / count
    for index, row in enumerate(rows):
        top = y0 + index * (height + gap)
        card.rect((x0, top, x1, top + height), WELL, 10)
        middle = top + height / 2
        left = x0 + 16
        if rank:
            card.text(left + 22, middle, str(row.get("rank", index + 1)), size * 0.82, bold=True, color=SOFT, anchor="mm")
            left += 56
        if index < len(swatches) and swatches[index] is not None:
            card.swatch(left + 8, middle, swatches[index], 16)
            left += 28
        if marker is not None:
            left += marker(card, row, left, middle, height)
        value = row.get("value_text", "")
        value_size = min(size, height * 0.6)
        value_width = card.measure(value, value_size, True)
        card.text(x1 - 16, middle, value, value_size, bold=True, anchor="rm")
        name_room = x1 - 16 - value_width - 18 - left
        color = TEXT if row.get("plain", True) else DIM
        name_size = min(size, height * 0.6)
        card.fitted(left, middle, row.get("label", ""), name_room, name_size, minimum=name_size * 0.85, color=color, anchor="lm")


def header(card: Card, *, title: str, subtitle: str, picture: bytes | None = None, letter: str = "", icon: str = "", chips: Sequence[tuple[str, str]] = (),
           round_picture: bool = True) -> float:
    """The top of a card: a picture or an icon, the title, a line under it, and labeled facts on the right. Returns where the next row starts."""
    left = 32
    top = 28
    side = 96
    if picture is not None or letter:
        card.picture(picture, (left, top, left + side, top + side), round_=round_picture, letter=letter, corner=22)
        text_left = left + side + 24
    elif icon:
        card.rect((left, top, left + side, top + side), PANEL, 24)
        card.icon(icon, left + side / 2, top + side / 2, 60, SOFT)
        text_left = left + side + 24
    else:
        text_left = left
    chip_room = 0.0
    chip_widths = []
    for label, value in chips:
        width = max(card.measure(label, 19), card.measure(value, 27, True)) + 36
        chip_widths.append(width)
        chip_room += width + 14
    right = WIDTH - 32
    x = right
    for (label, value), width in zip(reversed(chips), reversed(chip_widths)):
        card.rect((x - width, top + 8, x, top + 88), PANEL, 16)
        card.text(x - width / 2, top + 38, label, 19, color=DIM, anchor="ms")
        card.text(x - width / 2, top + 72, value, 27, bold=True, anchor="ms")
        x -= width + 14
    room = right - chip_room - text_left - 12
    card.fitted(text_left, top + 50, title, room, 46, bold=True, minimum=26)
    card.fitted(text_left, top + 90, subtitle, room, 27, color=SOFT, minimum=18)
    return top + side + 26


def footer(card: Card, left_text: str, right_text: str = "Captain Guild") -> None:
    baseline = card.height - 22
    width = card.measure(right_text, 22, True)
    card.fitted(32, baseline, left_text, WIDTH - 64 - width - 24, 22, color=DIM, minimum=15)
    card.text(WIDTH - 32, baseline, right_text, 22, bold=True, color=DIM, anchor="rs")


# ---- charts ----

def nice_step(span: float, count: int = 4) -> float:
    """A round step that cuts `span` into about `count` parts: 1, 2, 2.5 or 5 times a power of ten."""
    raw = max(span, 1e-9) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    for step in (1, 2, 2.5, 5, 10):
        if raw <= step * magnitude + 1e-12:
            return step * magnitude
    return 10 * magnitude


def nice_ticks(top: float, count: int = 4) -> list[float]:
    """Round steps from zero past `top`: 0, 250, 500, 750, 1,000."""
    if top <= 0:
        return [0.0, 1.0]
    size = nice_step(top, count)
    ticks = [0.0]
    while ticks[-1] < top - 1e-9:
        ticks.append(ticks[-1] + size)
    return ticks


def label_places(spots: Sequence[tuple[int, float, float]], left: float, right: float, *, gap: float = 24, ends: bool = True) -> list[tuple[int, float, str]]:
    """Where the chosen labels go: (index, x, anchor) for each (index, x, width) asked, left to right. With `ends`, the first bucket's label
    starts at the plot's left edge and the last one ends at its right edge, as a line's first and last points sit on those edges. A label
    that would come closer than `gap` to the one kept before it is left out, and the last label always stays, so a label beside it goes."""
    placed: list[tuple[int, float, str, float, float]] = []
    final = spots[-1][0] if spots else None
    for index, x, width in spots:
        anchor = "ms"
        start, end = x - width / 2, x + width / 2
        if ends and index == 0:
            anchor, start, end = "ls", x, x + width
        elif ends and index == final and len(spots) > 1:
            anchor, start, end = "rs", x - width, x
        elif start < left:
            anchor, x, start, end = "ls", left, left, left + width
        elif end > right:
            anchor, x, start, end = "rs", right, right - width, right
        while placed and start < placed[-1][4] + gap:
            if index != final:
                break
            placed.pop()  # the last label stays, so the one crowding it goes
        else:
            placed.append((index, x, anchor, start, end))
    return [(index, x, anchor) for index, x, anchor, _, _ in placed]


def label_indexes(count: int, width: float, label_width: float, most: int | None = None) -> list[int]:
    """Which buckets get a label under the axis, evenly spaced, the last one always. `most` caps how many, when the room alone allows more."""
    if count <= 0:
        return []
    most = max(1, min(most or count, int(width // (label_width + 24))))
    if count <= most:
        return list(range(count))
    step = math.ceil((count - 1) / max(most - 1, 1))
    chosen = list(range(count - 1, -1, -step))[::-1]
    return chosen


class Plot:
    """A chart's frame: the surface, the value axis on the left, the bucket labels along the bottom."""

    def __init__(self, card: Card, box: Sequence[float], top_value: float, labels: Sequence[str], fmt: Callable[[float], str], *,
                 bottom_value: float = 0.0, axis_labels: bool = True, columns: bool = False, surface: bool = True) -> None:
        self.card = card
        x0, y0, x1, y1 = box
        if surface:
            card.rect(box, WELL, 14)
        if bottom_value < 0:
            step = nice_step(max(top_value, 0) - bottom_value, 4)
            self.ticks = [step * index for index in range(0, max(1, math.ceil(max(top_value, 0) / step - 1e-9)) + 1)]
            self.bottom_ticks = [-step * index for index in range(1, math.ceil(-bottom_value / step - 1e-9) + 1)]
        else:
            self.ticks = nice_ticks(max(top_value, 1e-9)) if top_value > 0 else [0.0, 1.0]
            self.bottom_ticks = []
        self.high = self.ticks[-1]
        self.low = self.bottom_ticks[-1] if self.bottom_ticks else 0.0
        tick_texts = [fmt(abs(tick)) for tick in self.ticks + self.bottom_ticks]
        tick_width = max((card.measure(text, 20) for text in tick_texts), default=20) if axis_labels else 0
        self.left = x0 + 18 + tick_width + (12 if axis_labels else 0)
        self.right = x1 - 22
        self.top = y0 + 22
        self.bottom = y1 - (44 if labels else 16)
        self.count = max(len(labels), 1)
        self.columns = columns
        for tick, text in zip(self.ticks + self.bottom_ticks, tick_texts):
            y = self.y(tick)
            card.hairline(self.left, y, self.right, y, GRID if tick else AXIS, 1)
            if axis_labels:
                card.text(self.left - 12, y, text, 20, color=DIM, anchor="rm")
        if labels:
            widest = max(card.measure(text, 19) for text in labels)
            most = len(labels)
            while True:  # fewer labels until every one chosen fits, so they stay evenly spaced
                chosen = label_indexes(len(labels), self.right - self.left, widest, most)
                placed = label_places([(index, self.x(index), card.measure(labels[index], 19)) for index in chosen], self.left, self.right,
                                      ends=not columns)
                if len(placed) == len(chosen) or len(chosen) <= 2:
                    break
                most = len(chosen) - 1
            for index, x, anchor in placed:
                card.text(x, y1 - 14, labels[index], 19, color=DIM, anchor=anchor)

    def x(self, index: float) -> float:
        if self.columns:
            return self.left + (index + 0.5) * (self.right - self.left) / self.count
        if self.count <= 1:
            return (self.left + self.right) / 2
        return self.left + index * (self.right - self.left) / (self.count - 1)

    def y(self, value: float) -> float:
        span = self.high - self.low or 1
        return self.bottom - (value - self.low) / span * (self.bottom - self.top)

    def slot_width(self) -> float:
        return (self.right - self.left) / self.count


PARTIAL = 0.45  # how strongly the bucket still running is drawn


def _faded(color: str) -> tuple[int, int, int, int]:
    return (*rgb(color), round(255 * PARTIAL))


def area(plot: Plot, values: Sequence[float | None], color: str, *, wash: bool = True, end_dot: bool = True, partial: bool = False) -> None:
    """A line with a light wash under it. Gaps (None) break the line. With `partial` the last bucket is still running, so the segment into
    it is drawn lighter."""
    card = plot.card
    if partial and len(values) >= 3 and values[-1] is not None and values[-2] is not None:
        area(plot, values[:-1], color, wash=wash, end_dot=False)
        x0, y0 = plot.x(len(values) - 2), plot.y(values[-2])
        x1, y1 = plot.x(len(values) - 1), plot.y(values[-1])
        if wash:
            base = plot.y(max(plot.low, 0))
            card.polygon([(x0, y0), (x1, y1), (x1, base), (x0, base)], (*rgb(color), round(WASH * PARTIAL)))
        card.line([(x0, y0), (x1, y1)], _faded(color))
        if end_dot:
            card.dot(x1, y1, _faded(color))
        return
    runs: list[list[tuple[float, float]]] = [[]]
    for index, value in enumerate(values):
        if value is None:
            if runs[-1]:
                runs.append([])
            continue
        runs[-1].append((plot.x(index), plot.y(value)))
    base = plot.y(max(plot.low, 0))
    for points in runs:
        if not points:
            continue
        if len(points) == 1:
            card.dot(points[0][0], points[0][1], color, radius=DOT * 0.7)
            continue
        if wash:
            card.polygon([*points, (points[-1][0], base), (points[0][0], base)], (*rgb(color), WASH))
        card.line(points, color)
    last = next(((plot.x(index), plot.y(value)) for index, value in reversed(list(enumerate(values))) if value is not None), None)
    if end_dot and last is not None:
        card.dot(last[0], last[1], color)


def columns(plot: Plot, values: Sequence[float], color: Any, *, partial: bool = False) -> None:
    """One bar per bucket, at most 54 pixels wide, with the surface between neighbors. With `partial` the last bucket is still running and
    its bar is lighter."""
    card = plot.card
    width = min(plot.slot_width() - GAP, 54)
    base = plot.y(0)
    for index, value in enumerate(values):
        if not value:
            continue
        x = plot.x(index)
        if partial and index == len(values) - 1:
            color = _faded(color)
        if width < 2:
            card.hairline(x, base, x, plot.y(value), rgb(color), max(width, 1))
            continue
        top = plot.y(value)
        if value > 0:
            card.bar((x - width / 2, top, x + width / 2, base), color, CORNER)
        else:
            card.bar((x - width / 2, base, x + width / 2, top), color, CORNER, up=False)


def stacked_columns(plot: Plot, layers: Sequence[tuple[Sequence[float], str]]) -> None:
    """Bars whose parts stack, first layer at the bottom, with the surface gap between parts."""
    card = plot.card
    width = min(plot.slot_width() - GAP, 54)
    for index in range(plot.count):
        running = 0.0
        parts = [(values[index] if index < len(values) else 0, color) for values, color in layers]
        last_part = max((number for number, (value, _) in enumerate(parts) if value), default=-1)
        for number, (value, color) in enumerate(parts):
            if not value:
                continue
            low, high = plot.y(running), plot.y(running + value)
            running += value
            x = plot.x(index)
            if width < 2:
                card.hairline(x, low, x, high, rgb(color), max(width, 1))
                continue
            top_edge = high + (0 if number == last_part else GAP / 2)
            if number == last_part:
                card.bar((x - width / 2, top_edge, x + width / 2, low), color, CORNER)
            else:
                card.rect((x - width / 2, top_edge, x + width / 2, low), color)


def stacked_area(plot: Plot, layers: Sequence[tuple[Sequence[float | None], str]]) -> None:
    """Areas that stack, first layer at the bottom. Each band is filled with its color and outlined on top."""
    card = plot.card
    count = plot.count
    running = [0.0] * count
    tops: list[list[float]] = []
    for values, color in layers:
        top_values = [running[index] + (values[index] or 0) if index < len(values) else running[index] for index in range(count)]
        lower = [(plot.x(index), plot.y(running[index])) for index in range(count)]
        upper = [(plot.x(index), plot.y(top_values[index])) for index in range(count)]
        card.polygon(upper + lower[::-1], (*rgb(color), 70))
        tops.append(top_values)
        running = top_values
    for (values, color), top_values in zip(layers, tops):
        card.line([(plot.x(index), plot.y(top_values[index])) for index in range(count)], color)


def lines(plot: Plot, series: Sequence[tuple[Sequence[float], str]], *, partial: bool = False) -> None:
    for values, color in series:
        area(plot, values, color, wash=False, partial=partial)


def diverging_columns(plot: Plot, up: Sequence[float], down: Sequence[float], up_color: str = UP, down_color: str = DOWN, *,
                      partial: bool = False) -> None:
    """Joins above the line and leaves below it, one bar each per bucket."""
    columns(plot, list(up), up_color, partial=partial)
    columns(plot, [-value for value in down], down_color, partial=partial)


def donut(card: Card, center: tuple[float, float], radius: float, thickness: float, parts: Sequence[tuple[float, str]], *, ring: Any = PANEL) -> None:
    """A ring cut into parts by value, clockwise from the top, with the surface gap between them."""
    cx, cy = center
    total = sum(value for value, _ in parts if value > 0)
    box = ((cx - radius) * S, (cy - radius) * S, (cx + radius) * S, (cy + radius) * S)
    if total <= 0:
        card.draw.ellipse(box, fill=WELL)
    else:
        angle = -90.0
        edges = []
        for value, color in parts:
            if value <= 0:
                continue
            sweep = 360.0 * value / total
            card.draw.pieslice(box, angle, angle + sweep, fill=rgb(color))
            edges.append(angle)
            angle += sweep
        if len(edges) > 1:
            for start in edges:
                radians = math.radians(start)
                card.draw.line([(cx * S, cy * S), ((cx + (radius + 2) * math.cos(radians)) * S, (cy + (radius + 2) * math.sin(radians)) * S)],
                               fill=ring, width=max(1, round(GAP * S)))
    inner = radius - thickness
    card.draw.ellipse(((cx - inner) * S, (cy - inner) * S, (cx + inner) * S, (cy + inner) * S), fill=ring)


def legend(card: Card, x: float, y: float, items: Sequence[tuple[str, str]], *, size: float = 21, room: float | None = None, right: bool = False) -> float:
    """Swatches with their names in a row. Returns the width used. Drawn from the right edge when `right`."""
    widths = [28 + card.measure(name, size) + 22 for name, _ in items]
    total = sum(widths)
    if room is not None and total > room:
        scale_size = max(15, size * room / total)
        widths = [28 + card.measure(name, scale_size) + 18 for name, _ in items]
        total = sum(widths)
        size = scale_size
    start = x - total if right else x
    for (name, color), width in zip(items, widths):
        card.swatch(start + 8, y, color, 16)
        card.text(start + 26, y, name, size, color=SOFT, anchor="lm")
        start += width
    return total
