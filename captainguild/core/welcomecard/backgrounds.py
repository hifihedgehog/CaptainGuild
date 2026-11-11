"""Backgrounds drawn by code: gradients, stripes, and a catalog of stock pictures.

Every stock background is a function that draws itself at whatever size it is asked for, so one is as sharp on a 2000 pixel canvas as on a
thumbnail, and nothing here is cropped or stretched to fit. Shapes are drawn at twice the size and scaled down, which smooths their edges.
Anything scattered, such as stars or confetti, comes from a generator with a fixed seed, so a background is the same picture every time.
"""
from __future__ import annotations

import functools
import io
import math
import random
from dataclasses import dataclass
from typing import Callable, Sequence

from PIL import Image, ImageDraw, ImageFilter

from .model import rgba

THUMB = (240, 90)
SMOOTH_PIXELS = 1_500_000  # canvases up to this many pixels are drawn at twice their size

Color = tuple[int, int, int]


def rgb(text: str) -> Color:
    return rgba(text)[:3]


def _mix(first: Sequence[int], second: Sequence[int], share: float) -> tuple[int, ...]:
    return tuple(round(a + (b - a) * share) for a, b in zip(first, second))


def ramp(width: int, height: int, angle: float) -> Image.Image:
    """A gray picture that goes from black to white along an angle: 0 runs left to right, 90 top to bottom."""
    radians = math.radians(angle)
    dx, dy = math.cos(radians), math.sin(radians)
    reach = [x * dx + y * dy for x, y in ((0, 0), (width, 0), (0, height), (width, height))]
    low, span = min(reach), (max(reach) - min(reach)) or 1.0
    step = 254.0 / span
    return Image.linear_gradient("L").transform((width, height), Image.Transform.AFFINE, (0, 0, 128, dx * step, dy * step, 0.5 - low * step), resample=Image.Resampling.BILINEAR)


_RADIAL_EDGE = 181  # Pillow's radial gradient reaches 255 in its corners, and this at the middle of each side


@functools.lru_cache(maxsize=64)
def _falloff(strength: float) -> tuple[int, ...]:
    """Turns Pillow's radial gradient into light: full at the center, easing to nothing where the circle meets the square's sides."""
    table = []
    for level in range(256):
        near = max(0.0, 1 - level / _RADIAL_EDGE)
        table.append(min(255, round(255 * strength * near * near * (3 - 2 * near))))
    return tuple(table)


def _tables(colors: Sequence[Sequence[int]]) -> list[list[int]]:
    """For each channel, the 256 values a gray ramp maps to when the colors are spread evenly along it."""
    channels = len(colors[0])
    tables: list[list[int]] = [[] for _ in range(channels)]
    last = len(colors) - 1
    for level in range(256):
        at = level / 255 * last
        index = min(int(at), last - 1) if last else 0
        share = at - index if last else 0.0
        for channel in range(channels):
            low = colors[index][channel]
            high = colors[min(index + 1, last)][channel]
            tables[channel].append(round(low + (high - low) * share))
    return tables


def gradient(width: int, height: int, colors: Sequence[str], angle: float = 90) -> Image.Image:
    """A linear gradient through the colors, as RGBA so a color can be see-through."""
    stops = [rgba(color) for color in colors]
    gray = ramp(width, height, angle)
    return Image.merge("RGBA", [gray.point(table) for table in _tables(stops)])


def stripes(width: int, height: int, colors: Sequence[str], direction: str = "horizontal") -> Image.Image:
    """Bands of equal size. Horizontal bands are stacked from the top, like a flag, and vertical ones stand side by side."""
    image = Image.new("RGBA", (width, height), rgba(colors[0]))
    draw = ImageDraw.Draw(image)
    count = len(colors)
    room = width if direction == "vertical" else height
    for index, color in enumerate(colors):
        start, end = room * index // count, room * (index + 1) // count - 1
        if end < start:
            continue  # more colors than pixels: this band gets none
        draw.rectangle((start, 0, end, height) if direction == "vertical" else (0, start, width, end), fill=rgba(color))
    return image


# Ready-made color sets for stripes, in the order the picker shows them.
STRIPE_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Rainbow", ("#e40303", "#ff8c00", "#ffed00", "#008026", "#004dff", "#750787")),
    ("Sunset", ("#2d1b4e", "#6a2c70", "#b83b5e", "#f08a5d", "#f9ed69")),
    ("Ocean", ("#03045e", "#0077b6", "#00b4d8", "#90e0ef", "#caf0f8")),
    ("Forest", ("#1b4332", "#2d6a4f", "#40916c", "#74c69d", "#b7e4c7")),
    ("Candy", ("#ff70a6", "#ff9770", "#ffd670", "#e9ff70", "#70d6ff")),
    ("Berry", ("#3c096c", "#7b2cbf", "#c77dff", "#e0aaff")),
    ("Ember", ("#370617", "#9d0208", "#dc2f02", "#f48c06", "#ffba08")),
    ("Slate", ("#212529", "#343a40", "#495057", "#6c757d", "#adb5bd")),
    ("Trans", ("#5bcefa", "#f5a9b8", "#ffffff", "#f5a9b8", "#5bcefa")),
    ("Bi", ("#d60270", "#d60270", "#9b4f96", "#0038a8", "#0038a8")),
    ("Pan", ("#ff218c", "#ffd800", "#21b1ff")),
    ("Nonbinary", ("#fcf434", "#ffffff", "#9c59d1", "#2c2c2c")),
    ("Lesbian", ("#d52d00", "#ff9a56", "#ffffff", "#d362a4", "#a30262")),
    ("Ace", ("#000000", "#a3a3a3", "#ffffff", "#800080")),
)


class Scene:
    """One stock background being drawn. Coordinates are the finished picture's pixels."""

    def __init__(self, width: int, height: int, seed: int) -> None:
        self.w, self.h = width, height
        self.s = 2 if width * height <= SMOOTH_PIXELS else 1
        self.u = min(width, height)  # sizes of things are multiples of this, so they keep their shape on any canvas
        self.image = Image.new("RGB", (width * self.s, height * self.s), (0, 0, 0))
        self.draw = ImageDraw.Draw(self.image, "RGBA")
        self.random = random.Random(seed)

    # ---- fills ----

    def fill(self, colors: Sequence[str], angle: float = 90) -> None:
        if len(colors) == 1:
            self.draw.rectangle((0, 0, self.image.width, self.image.height), fill=rgb(colors[0]))
            return
        self.image.paste(gradient(self.image.width, self.image.height, colors, angle).convert("RGB"))

    def band(self, top: float, bottom: float, colors: Sequence[str], angle: float = 90) -> None:
        """A gradient across the full width between two heights."""
        y0, y1 = round(top * self.s), round(bottom * self.s)
        if y1 > y0:
            self.image.paste(gradient(self.image.width, y1 - y0, colors, angle).convert("RGB"), (0, y0))

    def glow(self, x: float, y: float, radius: float, color: str, strength: float = 1.0) -> None:
        """Light that is strongest at a point and fades to nothing at the radius."""
        size = max(2, round(2 * radius * self.s))
        mask = Image.radial_gradient("L").point(_falloff(strength)).resize((size, size), Image.Resampling.BICUBIC)
        self.image.paste(rgb(color), (round(x * self.s - size / 2), round(y * self.s - size / 2)), mask)

    def shade(self, strength: float = 0.5) -> None:
        """Darkens the corners, which keeps text in the middle easy to read."""
        size = (max(2, self.image.width // 4), max(2, self.image.height // 4))
        mask = Image.radial_gradient("L").resize(size, Image.Resampling.BICUBIC).point([round(max(0, level - 96) * 255 / 159 * strength) for level in range(256)])
        self.image.paste((0, 0, 0), (0, 0), mask.resize(self.image.size, Image.Resampling.BICUBIC))

    # ---- shapes ----

    def _points(self, points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * self.s, y * self.s) for x, y in points]

    def poly(self, points: Sequence[tuple[float, float]], color: str, alpha: int = 255) -> None:
        if len(points) >= 3:
            self.draw.polygon(self._points(points), fill=(*rgb(color), alpha))

    def rect(self, x0: float, y0: float, x1: float, y1: float, color: str, alpha: int = 255, radius: float = 0) -> None:
        if x1 <= x0 or y1 <= y0:
            return
        box = (x0 * self.s, y0 * self.s, x1 * self.s, y1 * self.s)
        if radius:
            self.draw.rounded_rectangle(box, radius=min(radius * self.s, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2), fill=(*rgb(color), alpha))
        else:
            self.draw.rectangle(box, fill=(*rgb(color), alpha))

    def disc(self, x: float, y: float, radius: float, color: str, alpha: int = 255) -> None:
        if radius > 0:
            self.draw.ellipse(((x - radius) * self.s, (y - radius) * self.s, (x + radius) * self.s, (y + radius) * self.s), fill=(*rgb(color), alpha))

    def ring(self, x: float, y: float, radius: float, color: str, width: float, alpha: int = 255) -> None:
        if radius > 0:
            self.draw.ellipse(((x - radius) * self.s, (y - radius) * self.s, (x + radius) * self.s, (y + radius) * self.s), outline=(*rgb(color), alpha), width=max(1, round(width * self.s)))

    def line(self, points: Sequence[tuple[float, float]], color: str, width: float, alpha: int = 255) -> None:
        if len(points) >= 2:
            self.draw.line(self._points(points), fill=(*rgb(color), alpha), width=max(1, round(width * self.s)), joint="curve")

    def below(self, heights: Sequence[float], color: str, alpha: int = 255) -> None:
        """Everything under a line of heights spread evenly across the width: a hill, a ridge, a wave."""
        count = len(heights) - 1
        points = [(self.w * index / count, height) for index, height in enumerate(heights)]
        self.poly([(0, self.h + 2), *points, (self.w, self.h + 2)], color, alpha)

    def soft(self, paint: Callable[["Scene"], None], blur: float, alpha: int = 255) -> None:
        """Draws through `paint` on a clear sheet, blurs the sheet and lays it over. The sheet is small, since a blur hides the difference."""
        small = Scene.__new__(Scene)
        small.w, small.h, small.u, small.random = self.w, self.h, self.u, self.random
        factor = max(1, round(blur * self.s / 3))
        small.s = self.s / factor
        small.image = Image.new("RGBA", (max(1, self.image.width // factor), max(1, self.image.height // factor)), (0, 0, 0, 0))
        small.draw = ImageDraw.Draw(small.image, "RGBA")
        paint(small)
        sheet = small.image.filter(ImageFilter.GaussianBlur(max(0.5, blur * self.s / factor))).resize(self.image.size, Image.Resampling.BICUBIC)
        mask = sheet.getchannel("A")
        if alpha != 255:
            mask = mask.point([level * alpha // 255 for level in range(256)])
        self.image.paste(sheet.convert("RGB"), (0, 0), mask)

    # ---- things scenes share ----

    def curve(self, base: float, swing: float, bumps: float, phase: float = 0.0, points: int = 96) -> list[float]:
        """Heights along a smooth rolling line."""
        return [base + swing * (math.sin(phase + bumps * math.tau * index / points) * 0.6 + math.sin(phase * 1.7 + bumps * 2.3 * math.tau * index / points) * 0.4) for index in range(points + 1)]

    def ridge(self, base: float, swing: float, steps: int = 6, keep: float = 0.62) -> list[float]:
        """Heights along a jagged line, by halving each stretch and nudging its middle. A higher `keep` leaves it rougher."""
        heights = [base + self.random.uniform(-swing, swing) * 0.5 for _ in range(3)]
        reach = swing
        for _ in range(steps):
            grown = []
            for first, second in zip(heights, heights[1:]):
                grown.extend((first, (first + second) / 2 + self.random.uniform(-reach, reach)))
            grown.append(heights[-1])
            heights, reach = grown, reach * keep
        return heights

    def stars(self, count: int, bottom: float, color: str = "#ffffff") -> None:
        for _ in range(count):
            x, y = self.random.uniform(0, self.w), self.random.uniform(0, bottom)
            size = self.random.choice((0.6, 0.6, 0.8, 0.8, 1.0, 1.4)) * max(1.0, self.u / 300)
            self.disc(x, y, size, color, self.random.randint(120, 255))

    def finish(self) -> Image.Image:
        if self.s == 1:
            return self.image
        return self.image.resize((self.w, self.h), Image.Resampling.LANCZOS)


# ---- the catalog ----

@dataclass(frozen=True)
class Stock:
    key: str
    name: str
    category: str
    paint: Callable[[Scene], None]


CATALOG: dict[str, Stock] = {}
CATEGORIES = ("Gradients", "Patterns", "Scenes", "Glow")


def stock(key: str, name: str, category: str) -> Callable[[Callable[[Scene], None]], Callable[[Scene], None]]:
    def register(paint: Callable[[Scene], None]) -> Callable[[Scene], None]:
        CATALOG[key] = Stock(key, name, category, paint)
        return paint
    return register


# Gradients

@stock("dusk", "Dusk", "Gradients")
def _dusk(s: Scene) -> None:
    s.fill(("#2b1055", "#7597de"), 35)
    s.glow(s.w * 0.85, s.h * 0.1, s.u * 1.1, "#ff8fab", 0.55)


@stock("ocean", "Ocean", "Gradients")
def _ocean(s: Scene) -> None:
    s.fill(("#031d44", "#04395e", "#0a9396", "#94d2bd"), 60)


@stock("ember", "Ember", "Gradients")
def _ember(s: Scene) -> None:
    s.fill(("#1a0000", "#7f0000", "#e85d04", "#ffba08"), 115)
    s.shade(0.45)


@stock("mint", "Mint", "Gradients")
def _mint(s: Scene) -> None:
    s.fill(("#0b3d2e", "#1f8a70", "#bfdb38"), 25)


@stock("candy", "Candy", "Gradients")
def _candy(s: Scene) -> None:
    s.fill(("#ff6fd8", "#b06ab3", "#3813c2"), 20)
    s.glow(s.w * 0.15, s.h * 0.9, s.u * 0.9, "#ffd6a5", 0.5)


@stock("slate", "Slate", "Gradients")
def _slate(s: Scene) -> None:
    s.fill(("#2f353a", "#3a4149", "#23282c"), 70)
    s.glow(s.w * 0.5, s.h * 0.3, s.u * 1.2, "#515b65", 0.6)
    s.shade(0.5)


@stock("peach", "Peach", "Gradients")
def _peach(s: Scene) -> None:
    s.fill(("#ffecd2", "#fcb69f", "#ff8177"), 100)


@stock("midnight", "Midnight", "Gradients")
def _midnight(s: Scene) -> None:
    s.fill(("#0f2027", "#203a43", "#2c5364"), 0)
    s.glow(s.w * 0.5, s.h * 1.1, s.u * 1.4, "#4b6cb7", 0.5)
    s.shade(0.6)


@stock("grape", "Grape", "Gradients")
def _grape(s: Scene) -> None:
    s.fill(("#12002b", "#4a0072", "#c2185b"), 140)
    s.glow(s.w * 0.2, s.h * 0.2, s.u * 0.9, "#7c4dff", 0.45)


@stock("sky", "Sky", "Gradients")
def _sky(s: Scene) -> None:
    s.fill(("#1e63c9", "#56a0f5", "#c9e6ff"), 90)
    s.glow(s.w * 0.8, s.h * 0.15, s.u * 0.8, "#ffffff", 0.5)


# Patterns

@stock("diagonal", "Diagonal stripes", "Patterns")
def _diagonal(s: Scene) -> None:
    s.fill(("#1f6feb", "#1a56c4"), 90)
    step = s.u * 0.16
    for index in range(-int(s.h / step) - 2, int(s.w / step) + 3):
        x = index * step
        s.poly([(x, 0), (x + step * 0.5, 0), (x + step * 0.5 + s.h, s.h), (x + s.h, s.h)], "#ffffff", 26)


@stock("dots", "Dots", "Patterns")
def _dots(s: Scene) -> None:
    s.fill(("#ff9f1c", "#ff6b35"), 90)
    step = s.u * 0.13
    for row in range(int(s.h / step) + 2):
        for column in range(int(s.w / step) + 2):
            s.disc((column + (row % 2) * 0.5) * step, row * step, step * 0.2, "#ffffff", 70)


@stock("blueprint", "Blueprint", "Patterns")
def _blueprint(s: Scene) -> None:
    s.fill(("#0b3c7a", "#0a2f61"), 90)
    step = s.u * 0.1
    for index in range(int(max(s.w, s.h) / step) + 2):
        heavy = index % 5 == 0
        s.line([(index * step, 0), (index * step, s.h)], "#ffffff", s.u * (0.006 if heavy else 0.003), 90 if heavy else 45)
        s.line([(0, index * step), (s.w, index * step)], "#ffffff", s.u * (0.006 if heavy else 0.003), 90 if heavy else 45)
    s.shade(0.35)


@stock("chevrons", "Chevrons", "Patterns")
def _chevrons(s: Scene) -> None:
    s.fill(("#2a9d8f",))
    step, tall = s.u * 0.22, s.u * 0.13
    for row in range(-1, int(s.h / tall) + 2):
        points = [(column * step / 2, row * tall + (tall * 0.5 if column % 2 else 0)) for column in range(int(2 * s.w / step) + 3)]
        s.line(points, "#e9f5db" if row % 2 else "#264653", tall * 0.38, 130)


@stock("honeycomb", "Honeycomb", "Patterns")
def _honeycomb(s: Scene) -> None:
    s.fill(("#f6aa1c", "#bc3908"), 60)
    side = s.u * 0.1
    wide, tall = math.sqrt(3) * side, 1.5 * side
    for row in range(-1, int(s.h / tall) + 2):
        for column in range(-1, int(s.w / wide) + 2):
            cx, cy = (column + (row % 2) * 0.5) * wide, row * tall
            points = [(cx + side * 0.9 * math.cos(math.radians(60 * corner - 30)), cy + side * 0.9 * math.sin(math.radians(60 * corner - 30))) for corner in range(6)]
            s.poly(points, "#ffffff", 20 + (row * 7 + column * 13) % 4 * 14)


@stock("checker", "Checker", "Patterns")
def _checker(s: Scene) -> None:
    s.fill(("#3d348b", "#7678ed"), 45)
    step = s.u * 0.18
    for row in range(int(s.h / step) + 1):
        for column in range(int(s.w / step) + 1):
            if (row + column) % 2:
                s.rect(column * step, row * step, (column + 1) * step, (row + 1) * step, "#ffffff", 28)


@stock("triangles", "Triangles", "Patterns")
def _triangles(s: Scene) -> None:
    s.fill(("#134074", "#0b2545"), 90)
    step = s.u * 0.2
    tall = step * math.sqrt(3) / 2
    palette = ("#8da9c4", "#eef4ed", "#13315c", "#3a7ca5")
    for row in range(-1, int(s.h / tall) + 2):
        for column in range(-1, int(2 * s.w / step) + 3):
            x, y = column * step / 2, row * tall
            up = (row + column) % 2 == 0
            points = [(x, y + tall), (x + step, y + tall), (x + step / 2, y)] if up else [(x, y), (x + step, y), (x + step / 2, y + tall)]
            s.poly(points, s.random.choice(palette), s.random.randint(18, 90))


@stock("confetti", "Confetti", "Patterns")
def _confetti(s: Scene) -> None:
    s.fill(("#fffcf2", "#f4efe1"), 90)
    palette = ("#ef476f", "#ffd166", "#06d6a0", "#118ab2", "#8338ec")
    count = max(40, min(260, s.w * s.h // 2400))
    for _ in range(count):
        x, y, size, turn = s.random.uniform(0, s.w), s.random.uniform(0, s.h), s.u * s.random.uniform(0.012, 0.03), s.random.uniform(0, math.tau)
        color = s.random.choice(palette)
        if s.random.random() < 0.4:
            s.disc(x, y, size * 0.7, color, 230)
        else:
            long, short = size * 1.6, size * 0.5
            corners = [(long, short), (-long, short), (-long, -short), (long, -short)]
            s.poly([(x + cx * math.cos(turn) - cy * math.sin(turn), y + cx * math.sin(turn) + cy * math.cos(turn)) for cx, cy in corners], color, 230)


@stock("waves", "Waves", "Patterns")
def _waves(s: Scene) -> None:
    s.fill(("#caf0f8", "#90e0ef"), 90)
    shades = ("#48cae4", "#00b4d8", "#0096c7", "#0077b6", "#023e8a", "#03045e")
    for index, shade in enumerate(shades):
        s.below(s.curve(s.h * (0.22 + 0.14 * index), s.u * 0.06, 2.2 + index * 0.35, index * 1.9), shade)


@stock("rings", "Rings", "Patterns")
def _rings(s: Scene) -> None:
    s.fill(("#2b2d42", "#1d1e2c"), 90)
    reach = math.hypot(s.w, s.h)
    step = s.u * 0.09
    for index in range(int(reach / step) + 1, 0, -1):
        s.ring(s.w * 0.12, s.h * 0.95, index * step, ("#ef233c", "#edf2f4", "#8d99ae")[index % 3], step * 0.22, 150)


@stock("plaid", "Plaid", "Patterns")
def _plaid(s: Scene) -> None:
    s.fill(("#9d0208",))
    step = s.u * 0.3
    for index in range(-1, int(max(s.w, s.h) / step) + 2):
        for offset, width, color, alpha in ((0.0, 0.34, "#03071e", 90), (0.5, 0.1, "#ffba08", 110), (0.72, 0.04, "#ffffff", 120)):
            at = (index + offset) * step
            s.rect(at, 0, at + width * step, s.h, color, alpha)
            s.rect(0, at, s.w, at + width * step, color, alpha)


@stock("bricks", "Bricks", "Patterns")
def _bricks(s: Scene) -> None:
    s.fill(("#3d2c2e",))
    tall, wide, gap = s.u * 0.1, s.u * 0.26, s.u * 0.012
    shades = ("#a44a3f", "#b55a4c", "#964036", "#ad5244")
    for row in range(int(s.h / tall) + 2):
        for column in range(-1, int(s.w / wide) + 2):
            x = (column + (row % 2) * 0.5) * wide
            s.rect(x + gap, row * tall + gap, x + wide - gap, (row + 1) * tall - gap, s.random.choice(shades), 255, s.u * 0.008)
    s.shade(0.4)


@stock("scales", "Scales", "Patterns")
def _scales(s: Scene) -> None:
    s.fill(("#005f73",))
    radius = s.u * 0.11
    shades = ("#0a9396", "#94d2bd", "#0a9396", "#e9d8a6")
    for row in range(int(s.h / (radius * 0.75)) + 3, -2, -1):
        for column in range(-1, int(s.w / (radius * 2)) + 2):
            x, y = (column * 2 + row % 2) * radius, row * radius * 0.75
            s.disc(x, y, radius, "#003845", 255)
            s.disc(x, y, radius * 0.9, shades[row % 4], 255)


@stock("memphis", "Memphis", "Patterns")
def _memphis(s: Scene) -> None:
    s.fill(("#fdf0d5",))
    palette = ("#c1121f", "#003049", "#669bbc", "#f4a261", "#2a9d8f")
    count = max(18, min(120, s.w * s.h // 5500))
    for _ in range(count):
        x, y, size, color = s.random.uniform(0, s.w), s.random.uniform(0, s.h), s.u * s.random.uniform(0.04, 0.08), s.random.choice(palette)
        kind = s.random.randrange(4)
        if kind == 0:
            s.ring(x, y, size, color, size * 0.28)
        elif kind == 1:
            turn = s.random.uniform(0, math.tau)
            s.poly([(x + size * math.cos(turn + corner * math.tau / 3), y + size * math.sin(turn + corner * math.tau / 3)) for corner in range(3)], color)
        elif kind == 2:
            s.line([(x + step * size * 0.5, y + (size * 0.4 if step % 2 else -size * 0.4)) for step in range(-3, 4)], color, size * 0.2)
        else:
            for dx in range(3):
                for dy in range(3):
                    s.disc(x + dx * size * 0.45, y + dy * size * 0.45, size * 0.1, color)


@stock("crosses", "Plus signs", "Patterns")
def _crosses(s: Scene) -> None:
    s.fill(("#22223b", "#4a4e69"), 90)
    step, arm, thick = s.u * 0.17, s.u * 0.035, s.u * 0.012
    for row in range(int(s.h / step) + 2):
        for column in range(int(s.w / step) + 2):
            x, y = (column + (row % 2) * 0.5) * step, row * step
            s.rect(x - arm, y - thick, x + arm, y + thick, "#f2e9e4", 120, thick)
            s.rect(x - thick, y - arm, x + thick, y + arm, "#f2e9e4", 120, thick)


# Scenes

@stock("hills", "Rolling hills", "Scenes")
def _hills(s: Scene) -> None:
    s.fill(("#74c0fc", "#d0ebff"), 90)
    s.glow(s.w * 0.8, s.h * 0.22, s.u * 0.5, "#fff3bf", 0.9)
    s.disc(s.w * 0.8, s.h * 0.22, s.u * 0.1, "#ffe066")
    for index, shade in enumerate(("#8ce99a", "#51cf66", "#37b24d", "#2b8a3e")):
        s.below(s.curve(s.h * (0.5 + 0.13 * index), s.u * 0.07, 1.1 + index * 0.5, index * 2.4), shade)


@stock("mountains", "Mountains", "Scenes")
def _mountains(s: Scene) -> None:
    s.fill(("#22223b", "#9a8c98", "#f2cc8f"), 90)
    s.glow(s.w * 0.5, s.h * 0.75, s.u * 0.9, "#ffd6a5", 0.7)
    for index, shade in enumerate(("#6d6875", "#4a4e69", "#2b2d42", "#14151f")):
        s.below(s.ridge(s.h * (0.45 + 0.15 * index), s.u * (0.26 - 0.04 * index)), shade)


@stock("night", "Night sky", "Scenes")
def _night(s: Scene) -> None:
    s.fill(("#03045e", "#10002b"), 90)
    s.stars(max(40, min(400, s.w * s.h // 1800)), s.h)
    s.glow(s.w * 0.8, s.h * 0.25, s.u * 0.5, "#cfd8ff", 0.5)
    s.disc(s.w * 0.8, s.h * 0.25, s.u * 0.11, "#f8f9fa")
    s.disc(s.w * 0.8 + s.u * 0.045, s.h * 0.25 - s.u * 0.02, s.u * 0.095, "#131350")
    s.below(s.curve(s.h * 0.9, s.u * 0.04, 1.5, 0.7), "#0b0b2a")


@stock("sunset", "Sunset on the sea", "Scenes")
def _sunset(s: Scene) -> None:
    horizon = s.h * 0.62
    s.band(0, horizon, ("#3d1e6d", "#c9184a", "#ff8500", "#ffd60a"))
    s.band(horizon, s.h, ("#7b2c4f", "#1d1b3a"))
    s.glow(s.w * 0.5, horizon, s.u * 0.75, "#fff1a8", 0.85)
    s.disc(s.w * 0.5, horizon, s.u * 0.17, "#fff3b0")
    s.band(horizon, s.h, ("#7b2c4f", "#1d1b3a"))
    for index in range(9):
        y = horizon + (s.h - horizon) * (index + 0.5) / 9
        half = s.u * (0.2 + 0.035 * index) * (0.75 if index % 2 else 1)
        s.rect(s.w * 0.5 - half, y - s.u * 0.006, s.w * 0.5 + half, y + s.u * 0.006, "#ffd60a", 190 - index * 14, s.u * 0.006)


@stock("city", "City at night", "Scenes")
def _city(s: Scene) -> None:
    s.fill(("#0b132b", "#3a506b"), 90)
    s.stars(max(20, min(160, s.w * s.h // 4500)), s.h * 0.5)
    for layer, (shade, lit) in enumerate((("#1c2541", 0), ("#0b132b", 150))):
        x = -s.u * 0.05
        while x < s.w:
            wide = s.u * s.random.uniform(0.09, 0.2)
            top = s.h * s.random.uniform(0.3 + 0.12 * layer, 0.75)
            s.rect(x, top, x + wide, s.h, shade)
            if lit:
                step = s.u * 0.035
                for row in range(int((s.h - top) / step)):
                    for column in range(int(wide / step)):
                        if s.random.random() < 0.3:
                            s.rect(x + (column + 0.3) * step, top + (row + 0.35) * step, x + (column + 0.7) * step, top + (row + 0.75) * step, "#ffd166", lit)
            x += wide + s.u * 0.01


@stock("desert", "Desert dunes", "Scenes")
def _desert(s: Scene) -> None:
    s.fill(("#ffb347", "#ffd9a0"), 90)
    s.glow(s.w * 0.25, s.h * 0.3, s.u * 0.6, "#fff4d6", 0.9)
    s.disc(s.w * 0.25, s.h * 0.3, s.u * 0.12, "#fff8e7")
    for index, shade in enumerate(("#e9a15b", "#d68c45", "#bc6c25", "#99582a")):
        s.below(s.curve(s.h * (0.55 + 0.12 * index), s.u * 0.09, 0.8 + index * 0.3, index * 1.3 + 0.5), shade)


@stock("pines", "Pine forest", "Scenes")
def _pines(s: Scene) -> None:
    s.fill(("#cfe8e0", "#f1faee"), 90)
    for layer, shade in enumerate(("#a3c4bc", "#5f8f85", "#2f5d50", "#1b3a34")):
        base = s.h * (0.55 + 0.15 * layer)
        tall = s.u * (0.22 + 0.1 * layer)
        x = -tall * 0.3
        while x < s.w + tall:
            height = tall * s.random.uniform(0.7, 1.2)
            s.poly([(x - height * 0.22, base), (x + height * 0.22, base), (x, base - height)], shade)
            x += tall * s.random.uniform(0.22, 0.4)
        s.rect(0, base - 1, s.w, s.h, shade)


@stock("snow", "Snowfall", "Scenes")
def _snow(s: Scene) -> None:
    s.fill(("#14213d", "#3a5a80"), 90)
    for index, shade in enumerate(("#98c1d9", "#e0fbfc", "#ffffff")):
        s.below(s.curve(s.h * (0.68 + 0.11 * index), s.u * 0.05, 1.3 + index * 0.4, index * 2.1), shade)
    for _ in range(max(40, min(320, s.w * s.h // 2200))):
        s.disc(s.random.uniform(0, s.w), s.random.uniform(0, s.h), s.u * s.random.uniform(0.004, 0.013), "#ffffff", s.random.randint(140, 255))


@stock("space", "Ringed planet", "Scenes")
def _space(s: Scene) -> None:
    s.fill(("#0b0c1a", "#1b1d3a"), 60)
    s.stars(max(50, min(420, s.w * s.h // 1700)), s.h)
    x, y, radius = s.w * 0.74, s.h * 0.5, s.u * 0.3
    s.glow(x, y, radius * 2.2, "#7b5cff", 0.45)
    s.disc(x, y, radius, "#f4a261")
    for index, shade in enumerate(("#e76f51", "#e9c46a", "#e76f51")):
        offset = (index - 1) * radius * 0.42
        half = math.sqrt(max(0.0, radius ** 2 - offset ** 2))
        s.rect(x - half, y + offset - radius * 0.07, x + half, y + offset + radius * 0.07, shade, 160, radius * 0.07)
    box = ((x - radius * 1.7) * s.s, (y - radius * 0.32) * s.s, (x + radius * 1.7) * s.s, (y + radius * 0.32) * s.s)
    s.draw.arc(box, 0, 180, fill=(233, 216, 166, 230), width=max(1, round(radius * 0.07 * s.s)))


@stock("clouds", "Clouds", "Scenes")
def _clouds(s: Scene) -> None:
    s.fill(("#4dabf7", "#a5d8ff"), 90)
    for _ in range(max(4, min(12, s.w // 160))):
        x, y, size = s.random.uniform(0, s.w), s.random.uniform(s.h * 0.1, s.h * 0.85), s.u * s.random.uniform(0.07, 0.14)
        for dx, dy, scale in ((-1.3, 0.25, 0.8), (-0.5, -0.25, 1.05), (0.4, -0.05, 0.9), (1.2, 0.3, 0.7), (0.0, 0.35, 1.0)):
            s.disc(x + dx * size, y + dy * size, size * scale, "#ffffff", 235)


@stock("beach", "Beach", "Scenes")
def _beach(s: Scene) -> None:
    s.band(0, s.h * 0.45, ("#48cae4", "#ade8f4"))
    s.band(s.h * 0.45, s.h, ("#f4d58d", "#edc373"))
    s.disc(s.w * 0.82, s.h * 0.18, s.u * 0.1, "#fff3b0")
    for index, shade in enumerate(("#0077b6", "#0096c7", "#48cae4", "#ffffff")):
        s.below(s.curve(s.h * (0.42 + 0.06 * index), s.u * 0.02, 3 + index, index * 1.1), shade, 255 if index < 3 else 200)
    s.below(s.curve(s.h * 0.68, s.u * 0.025, 2.5, 4.0), "#f4d58d")


@stock("lake", "Mountain lake", "Scenes")
def _lake(s: Scene) -> None:
    shore = s.h * 0.58
    s.band(0, shore, ("#ffafcc", "#ffd6a5", "#fdffb6"))
    for index, shade in enumerate(("#b5838d", "#6d6875", "#355070")):
        heights = s.ridge(shore - s.u * (0.3 - 0.09 * index), s.u * 0.12)
        count = len(heights) - 1
        s.poly([(0, shore), *[(s.w * step / count, min(height, shore)) for step, height in enumerate(heights)], (s.w, shore)], shade)
        s.poly([(0, shore), *[(s.w * step / count, min(s.h, 2 * shore - min(height, shore))) for step, height in enumerate(heights)], (s.w, shore)], shade, 110)
    s.rect(0, shore, s.w, s.h, "#355070", 120)
    for index in range(8):
        y = shore + (s.h - shore) * (index + 0.6) / 8
        s.rect(s.w * s.random.uniform(0, 0.6), y, s.w * s.random.uniform(0.5, 1), y + s.u * 0.004, "#ffffff", 50)


@stock("meadow", "Meadow at sunrise", "Scenes")
def _meadow(s: Scene) -> None:
    s.fill(("#ff9e6d", "#ffd3a5", "#fdf5c9"), 90)
    s.glow(s.w * 0.5, s.h * 0.62, s.u * 0.8, "#fffbe0", 0.9)
    s.disc(s.w * 0.5, s.h * 0.62, s.u * 0.14, "#fffbe0")
    s.below(s.curve(s.h * 0.66, s.u * 0.03, 1.2, 0.4), "#95d5b2")
    s.below(s.curve(s.h * 0.78, s.u * 0.035, 1.7, 2.2), "#52b788")
    for _ in range(max(20, min(160, s.w * s.h // 4000))):
        x, y = s.random.uniform(0, s.w), s.random.uniform(s.h * 0.8, s.h)
        s.disc(x, y, s.u * 0.008, s.random.choice(("#ffffff", "#ffd166", "#ff8fab")))


# Glow

@stock("aurora", "Aurora", "Glow")
def _aurora(s: Scene) -> None:
    s.fill(("#020024", "#0b2545"), 90)
    s.stars(max(40, min(300, s.w * s.h // 2400)), s.h * 0.8)

    def bands(sheet: Scene) -> None:
        for index, shade in enumerate(("#2dc653", "#80ffdb", "#7b2cbf")):
            heights = sheet.curve(s.h * (0.3 + 0.12 * index), s.u * 0.12, 1.3 + index * 0.4, index * 2.0)
            count = len(heights) - 1
            upper = [(s.w * step / count, height) for step, height in enumerate(heights)]
            lower = [(x, y + s.u * 0.16) for x, y in reversed(upper)]
            sheet.poly(upper + lower, shade, 200)
    s.soft(bands, s.u * 0.05, 215)
    s.below(s.ridge(s.h * 0.9, s.u * 0.07, 6), "#01010f")


@stock("bokeh", "Bokeh", "Glow")
def _bokeh(s: Scene) -> None:
    s.fill(("#1a1423", "#3d314a"), 40)
    palette = ("#ffb4a2", "#e5989b", "#ffd6a5", "#b5838d", "#ffcdb2")

    def lights(sheet: Scene) -> None:
        for _ in range(max(14, min(70, s.w * s.h // 8000))):
            sheet.disc(s.random.uniform(0, s.w), s.random.uniform(0, s.h), s.u * s.random.uniform(0.04, 0.14), s.random.choice(palette), s.random.randint(70, 170))
    s.soft(lights, s.u * 0.012)
    s.shade(0.4)


@stock("mesh", "Color mesh", "Glow")
def _mesh(s: Scene) -> None:
    s.fill(("#4361ee",))
    for x, y, color in ((0.1, 0.15, "#f72585"), (0.9, 0.2, "#4cc9f0"), (0.25, 0.95, "#7209b7"), (0.8, 0.9, "#ffd60a"), (0.5, 0.5, "#3a0ca3")):
        s.glow(s.w * x, s.h * y, max(s.w, s.h) * 0.6, color, 0.9)


@stock("spotlight", "Spotlight", "Glow")
def _spotlight(s: Scene) -> None:
    s.fill(("#101014",))
    s.glow(s.w * 0.5, s.h * 0.45, max(s.w, s.h) * 0.6, "#3a86ff", 0.75)
    s.glow(s.w * 0.5, s.h * 0.45, s.u * 0.7, "#bde0fe", 0.5)
    s.shade(0.7)


@stock("synthwave", "Synthwave", "Glow")
def _synthwave(s: Scene) -> None:
    horizon = s.h * 0.6
    s.band(0, horizon, ("#10002b", "#5a189a", "#ff6d00"))
    s.band(horizon, s.h, ("#240046", "#10002b"))
    radius = s.u * 0.3
    s.glow(s.w * 0.5, horizon, radius * 2.4, "#ff9e00", 0.5)
    s.disc(s.w * 0.5, horizon, radius, "#ffbd00")
    s.glow(s.w * 0.5, horizon - radius, radius * 1.5, "#ff0054", 0.6)
    for index in range(5):
        y = horizon - radius * (0.12 + 0.17 * index)
        s.rect(s.w * 0.5 - radius, y, s.w * 0.5 + radius, y + radius * (0.1 - 0.014 * index), "#5a189a")
    s.band(horizon, s.h, ("#240046", "#10002b"))
    for index in range(1, 8):
        y = horizon + (s.h - horizon) * (index / 7) ** 2
        s.line([(0, y), (s.w, y)], "#ff48c4", s.u * 0.005, 200)
    for index in range(-12, 13):
        s.line([(s.w * 0.5 + index * s.w * 0.03, horizon), (s.w * 0.5 + index * s.w * 0.22, s.h)], "#ff48c4", s.u * 0.005, 200)


@stock("rays", "Sun rays", "Glow")
def _rays(s: Scene) -> None:
    s.fill(("#ffb703", "#fb8500"), 90)
    reach = math.hypot(s.w, s.h)
    x, y = s.w * 0.5, s.h * 1.05
    for index in range(24):
        if index % 2:
            first, second = math.tau * index / 24, math.tau * (index + 1) / 24
            s.poly([(x, y), (x + reach * math.cos(first), y + reach * math.sin(first)), (x + reach * math.cos(second), y + reach * math.sin(second))], "#fff3b0", 80)
    s.glow(x, y, s.u * 1.1, "#fff8dc", 0.9)


@stock("ribbons", "Ribbons", "Glow")
def _ribbons(s: Scene) -> None:
    s.fill(("#0d1b2a", "#1b263b"), 90)

    def bands(sheet: Scene) -> None:
        for index, shade in enumerate(("#f72585", "#7209b7", "#4361ee", "#4cc9f0")):
            heights = sheet.curve(s.h * (0.25 + 0.17 * index), s.u * 0.16, 0.9 + index * 0.25, index * 1.4 + 0.3)
            count = len(heights) - 1
            upper = [(s.w * step / count, height) for step, height in enumerate(heights)]
            lower = [(x, y + s.u * 0.1) for x, y in reversed(upper)]
            sheet.poly(upper + lower, shade, 215)
    s.soft(bands, s.u * 0.012)


@stock("nebula", "Nebula", "Glow")
def _nebula(s: Scene) -> None:
    s.fill(("#0b0014", "#1a0033"), 30)
    for _ in range(9):
        s.glow(s.random.uniform(0, s.w), s.random.uniform(0, s.h), s.u * s.random.uniform(0.5, 1.1), s.random.choice(("#7400b8", "#5390d9", "#e0218a", "#48bfe3")), 0.42)
    s.stars(max(60, min(480, s.w * s.h // 1500)), s.h)
    s.shade(0.5)


@stock("neon", "Neon lines", "Glow")
def _neon(s: Scene) -> None:
    s.fill(("#0a0a12",))
    lines = []
    for _ in range(7):
        y = s.random.uniform(0.1, 0.9) * s.h
        lines.append(([(0, y), (s.w * 0.35, y), (s.w * 0.35 + s.u * 0.15, y - s.u * 0.15), (s.w, y - s.u * 0.15)], s.random.choice(("#00f5d4", "#f15bb5", "#fee440", "#00bbf9"))))

    def halo(sheet: Scene) -> None:
        for points, color in lines:
            sheet.line(points, color, s.u * 0.03, 200)
    s.soft(halo, s.u * 0.025)
    for points, color in lines:
        s.line(points, color, s.u * 0.007)
        s.line(points, "#ffffff", s.u * 0.002, 190)


@stock("prism", "Prism", "Glow")
def _prism(s: Scene) -> None:
    s.fill(("#111827",))
    palette = ("#ef4444", "#f59e0b", "#10b981", "#3b82f6", "#8b5cf6", "#ec4899")
    wide = s.w / len(palette) * 1.35
    for index, color in enumerate(palette):
        x = s.w * index / len(palette) - s.h * 0.25
        s.poly([(x, s.h), (x + wide, s.h), (x + wide + s.h * 0.5, 0), (x + s.h * 0.5, 0)], color, 120)
    s.shade(0.45)


@stock("embers", "Embers", "Glow")
def _embers(s: Scene) -> None:
    s.fill(("#0d0d0d", "#2b0a0a"), 90)
    s.glow(s.w * 0.5, s.h * 1.2, max(s.w, s.h) * 0.7, "#ff4800", 0.7)

    def sparks(sheet: Scene) -> None:
        for _ in range(max(30, min(200, s.w * s.h // 3500))):
            rise = s.random.random()
            sheet.disc(s.random.uniform(0, s.w), s.h * (1 - rise), s.u * s.random.uniform(0.004, 0.012), s.random.choice(("#ffba08", "#ff7b00", "#ffd60a")), round(255 * (1 - rise * 0.7)))
    s.soft(sparks, s.u * 0.004)


# ---- drawing one ----

def names() -> list[dict]:
    """The catalog for the page's picker, in the order it shows them."""
    return [{"key": item.key, "name": item.name, "category": item.category} for category in CATEGORIES for item in CATALOG.values() if item.category == category]


def _seed(key: str) -> int:
    return sum((index + 1) * ord(letter) for index, letter in enumerate(key)) * 7919


def draw(key: str, width: int, height: int) -> Image.Image | None:
    """A stock background at this size, as RGB. None for a name the catalog doesn't have."""
    item = CATALOG.get(key)
    if item is None:
        return None
    scene = Scene(max(1, width), max(1, height), _seed(key))
    item.paint(scene)
    return scene.finish()


@functools.lru_cache(maxsize=128)
def thumbnail(key: str) -> bytes | None:
    """A small PNG of a stock background, for the picker."""
    image = draw(key, *THUMB)
    if image is None:
        return None
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()
