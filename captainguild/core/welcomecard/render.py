"""Draws a design as a PNG, or as a GIF when its background is an animated upload.

    picture = render(design, Inputs(texts=..., avatar=..., pictures=...))

`design` has been through model.clean_design. Everything else a picture needs arrives in `Inputs` as plain bytes and text, so this never
touches Discord or the database and gives the same bytes for the same inputs. It runs on a worker thread.

Layers are drawn from the bottom up onto a clear sheet the size of the canvas. Shapes get smooth edges from masks drawn at three times
their size and scaled down, the way the rank card draws its shapes. The sheet then goes over the background, or over every frame of it when
the background moves.

The renderer keeps its own limits. It stops when its time is up, thins an animation's frames and then shrinks it to stay under the byte
limit, and falls back from an animation to a still picture before it gives up. Every uploaded or fetched image passes the checks in
core/images.py before it is decoded.
"""
from __future__ import annotations

import colorsys
import io
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from PIL import Image, ImageChops, ImageDraw, ImageOps

from .. import images
from ..validate import Invalid
from . import backgrounds, gif, text as texts
from .model import rgba

SUPER = 3                          # shape masks are drawn at this many times their size
SUPER_PIXELS = 600_000             # above this many pixels a mask is drawn at twice its size instead
MAX_FRAMES = 60                    # frames in one animation, after thinning
MAX_FRAME_PIXELS = 30_000_000      # frames times canvas pixels in one animation
MAX_SOURCE_FRAMES = 2000           # frames read from an uploaded GIF before the rest are left out
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
STILL_SECONDS = 6.0
ANIMATED_SECONDS = 10.0
GRACE_SECONDS = 2.0                # what a still picture gets once an animation has run out of time
STILL_WRITE_SECONDS = 1.0          # writing the heaviest still picture took 0.63 s, so an animation in hand stops this long before the limit
CACHE_BYTES = 96 * 1024 * 1024     # prepared backgrounds and pictures kept between draws
SHORTEST_FRAME = 20                # milliseconds. A GIF frame set shorter than this plays at a tenth of a second in browsers.
LONGEST_FRAME = 60_000             # milliseconds. A GIF can hold a frame of at most 655 seconds, and a minute is long enough to show anything.
BLANK_AVATAR = (72, 82, 92)
STAND_IN_COLORS = ((64, 120, 214), (87, 158, 91), (214, 141, 44), (201, 77, 96), (61, 148, 172), (140, 98, 196))


log = logging.getLogger(__name__)

UNDRAWABLE = "This design couldn't be drawn. Undo the last change, or start again from a layout."
SLOW_BACKGROUND = "The animated background took too long to read. Use a shorter or smaller animation."


class RenderError(Exception):
    """The picture couldn't be drawn. The text says why, for the page."""


class _OutOfTime(Exception):
    pass


@dataclass(frozen=True)
class Raw:
    """A picture as it was uploaded or fetched. `key` names it for the cache of prepared pictures, and is empty for one not worth keeping, such as one member's avatar."""
    data: bytes
    key: str = ""


@dataclass
class Inputs:
    texts: dict[str, texts.Marked] = field(default_factory=dict)  # by text layer ID, filled in by the template language
    avatar: Raw | None = None
    pictures: dict[str, Raw] = field(default_factory=dict)        # "upload:<id>", "address:<layer id>" and "emoji:<id>"
    person: int = 0                 # the member's ID, which picks the color of the stand-in avatar
    animate: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES
    seconds: float | None = None    # the most time the draw may take, when the caller has less to give than the renderer's own limits


@dataclass
class Picture:
    data: bytes
    kind: str                       # "png" or "gif"
    width: int
    height: int
    frames: int = 1
    notes: list[str] = field(default_factory=list)       # what didn't go as designed, in sentences for the page
    sizes: dict[str, int] = field(default_factory=dict)  # the size each text layer was drawn at, by layer ID

    @property
    def filename(self) -> str:
        return f"welcome.{self.kind}"


# ---- pictures kept between draws ----

class _Cache:
    """Prepared pictures by key, the least recently used dropped first once they pass a byte limit. Worker threads share it."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.items: OrderedDict[tuple, tuple[Any, int]] = OrderedDict()
        self.held = 0
        self.lock = threading.Lock()

    def get(self, key: tuple) -> Any:
        with self.lock:
            found = self.items.get(key)
            if found is None:
                return None
            self.items.move_to_end(key)
            return found[0]

    def put(self, key: tuple, value: Any, size: int) -> None:
        if size > self.limit // 2:
            return
        with self.lock:
            old = self.items.pop(key, None)
            if old is not None:
                self.held -= old[1]
            self.items[key] = (value, size)
            self.held += size
            while self.held > self.limit and self.items:
                _, (_, dropped) = self.items.popitem(last=False)
                self.held -= dropped

    def clear(self) -> None:
        with self.lock:
            self.items.clear()
            self.held = 0


CACHE = _Cache(CACHE_BYTES)


def _bytes_of(image: Image.Image) -> int:
    return image.width * image.height * len(image.getbands())


# ---- reading pictures ----

def _open(data: bytes) -> Image.Image:
    """An uploaded or fetched picture, checked before it is decoded. Raises Invalid or one of images.READ_ERRORS."""
    images.check(data, min_side=1)
    return Image.open(io.BytesIO(data), formats=images.OPENED_FORMATS)


def _still(raw: Raw | None) -> Image.Image | None:
    """A picture's first frame as RGBA, or None when there is no picture or it can't be read."""
    if raw is None or not raw.data:
        return None
    key = ("still", raw.key)
    if raw.key:
        found = CACHE.get(key)
        if found is not None:
            return found
    try:
        with _open(raw.data) as image:
            if image.format == "GIF":
                picture = gif.first(raw.data)
            else:
                image.seek(0)
                picture = ImageOps.exif_transpose(image).convert("RGBA")
    except (Invalid, *images.READ_ERRORS):
        return None
    if raw.key:
        CACHE.put(key, picture, _bytes_of(picture))
    return picture


def is_animated(data: bytes) -> bool:
    """Whether a checked picture has more than one frame."""
    try:
        with _open(data) as image:
            return getattr(image, "n_frames", 1) > 1
    except (Invalid, *images.READ_ERRORS):
        return False


def _seeked(image: Image.Image, count: int, check: Callable[[], None]) -> Iterator[tuple[int, Image.Image]]:
    """The frames of an animated picture that isn't a GIF, as Pillow lays them, each with how long it shows in milliseconds."""
    for index in range(count):
        check()
        image.seek(index)
        yield image.info.get("duration") or 0, image


def _cover(picture: Image.Image, width: int, height: int) -> Image.Image:
    """The picture scaled to cover the box and cropped from its center."""
    return ImageOps.fit(picture, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def _contain(picture: Image.Image, width: int, height: int) -> Image.Image:
    scale = min(width / picture.width, height / picture.height)
    size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
    tile = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    tile.alpha_composite(picture.resize(size, Image.Resampling.LANCZOS), ((width - size[0]) // 2, (height - size[1]) // 2))
    return tile


# ---- the avatar ----

def stand_in_avatar(person: int, side: int = 256) -> Image.Image:
    """The picture drawn for someone whose avatar couldn't be read: a head and shoulders on one of six colors, picked by their ID."""
    color = STAND_IN_COLORS[(person >> 22) % len(STAND_IN_COLORS)] if person else BLANK_AVATAR
    large = side * 2
    image = Image.new("RGBA", (large, large), (*color, 255))
    draw = ImageDraw.Draw(image)
    light = tuple(round(channel + (255 - channel) * 0.82) for channel in color)
    draw.ellipse((large * 0.33, large * 0.2, large * 0.67, large * 0.54), fill=(*light, 255))
    draw.ellipse((large * 0.17, large * 0.62, large * 0.83, large * 1.3), fill=(*light, 255))
    return image.resize((side, side), Image.Resampling.LANCZOS)


def dominant_color(picture: Image.Image) -> tuple[int, int, int]:
    """The color an avatar is mostly made of, leaning toward its vivid colors, so a logo on white gives the logo's color and not gray."""
    data = picture.convert("RGBA").resize((40, 40), Image.Resampling.BOX).tobytes()
    seen = bytearray()
    for at in range(0, len(data), 4):
        if data[at + 3] >= 128:  # what shows through a see-through avatar is no part of it
            seen += data[at:at + 3]
    if not seen:
        return BLANK_AVATAR
    reduced = Image.frombytes("RGB", (len(seen) // 3, 1), bytes(seen)).quantize(colors=8, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    palette = reduced.getpalette() or []
    best, best_score = BLANK_AVATAR, -1.0
    for count, index in sorted(reduced.getcolors() or [], key=lambda row: (-row[0], row[1])):
        if 3 * index + 2 >= len(palette):
            continue
        red, green, blue = palette[3 * index:3 * index + 3]
        hue, light, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
        extreme = 0.25 if light < 0.12 or light > 0.93 else 1.0  # near black and near white are an avatar's backdrop more often than its subject
        score = count * (0.3 + saturation) * extreme
        if score > best_score:
            best, best_score = (red, green, blue), score
    hue, light, saturation = colorsys.rgb_to_hls(*(channel / 255 for channel in best))
    light = min(0.78, max(0.2, light))  # white text with a dark outline reads on anything in this range
    return tuple(round(channel * 255) for channel in colorsys.hls_to_rgb(hue, light, saturation))  # type: ignore[return-value]


# ---- shapes ----

def _scale_for(width: int, height: int) -> int:
    return SUPER if width * height <= SUPER_PIXELS else 2


def _round_box(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], radii: list[float]) -> None:
    """A filled box with a radius of its own at each corner: top left, top right, bottom right, bottom left."""
    x0, y0, x1, y1 = box
    wide, tall = x1 - x0, y1 - y0
    if wide <= 0 or tall <= 0:
        return
    top_left, top_right, bottom_right, bottom_left = (max(0.0, radius) for radius in radii)
    sums = (top_left + top_right, bottom_left + bottom_right, top_left + bottom_left, top_right + bottom_right)
    rooms = (wide, wide, tall, tall)
    shrink = min([1.0] + [room / total for room, total in zip(rooms, sums) if total > room])  # two corners can't take more than the side they share
    top_left, top_right, bottom_right, bottom_left = (radius * shrink for radius in (top_left, top_right, bottom_right, bottom_left))
    if top_left == top_right == bottom_right == bottom_left:
        draw.rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=top_left, fill=255)
        return
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    corners = (
        (top_left, x0, y0, x0, y0, 180), (top_right, x1 - top_right, y0, x1 - 2 * top_right, y0, 270),
        (bottom_right, x1 - bottom_right, y1 - bottom_right, x1 - 2 * bottom_right, y1 - 2 * bottom_right, 0), (bottom_left, x0, y1 - bottom_left, x0, y1 - 2 * bottom_left, 90),
    )
    for radius, square_x, square_y, circle_x, circle_y, start in corners:
        if radius >= 1:  # a corner under a pixel is square
            draw.rectangle((square_x, square_y, square_x + radius - 1, square_y + radius - 1), fill=0)
            draw.pieslice((circle_x, circle_y, circle_x + 2 * radius - 1, circle_y + 2 * radius - 1), start, start + 90, fill=255)


def shape_mask(shape: str, width: int, height: int, radii: list[int] | None = None, inset: float = 0) -> Image.Image:
    """A shape's coverage as a gray picture the size of its box. `inset` pulls every edge in, which is how a stroke's inner edge is made."""
    scale = _scale_for(width, height)
    large = Image.new("L", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(large)
    edge = inset * scale
    box = (edge, edge, width * scale - edge, height * scale - edge)
    if box[2] - box[0] >= 1 and box[3] - box[1] >= 1:
        if shape == "ellipse":
            draw.ellipse((box[0], box[1], box[2] - 1, box[3] - 1), fill=255)
        elif shape == "hexagon":
            corners = _hexagon(width * scale, height * scale, edge)
            if corners:
                draw.polygon(corners, fill=255)
        else:
            _round_box(draw, box, [max(0.0, (radius - inset) * scale) for radius in (radii or [0, 0, 0, 0])])
    return large.reduce(scale)


def _hexagon(width: float, height: float, inset: float) -> list[tuple[float, float]]:
    """The corners of a hexagon with a point at the top and the bottom and flat sides at the left and right, filling a box of this size, with
    every side moved in by `inset` along its own square. A ring is then as thick across a slanted side as across a flat one. Empty when the
    inset leaves nothing."""
    center_x, center_y = width / 2, height / 2
    reach_x, reach_y = width / 2 / math.cos(math.radians(30)), height / 2
    outer = [(center_x + reach_x * math.cos(math.radians(60 * corner - 90)), center_y + reach_y * math.sin(math.radians(60 * corner - 90))) for corner in range(6)]
    if not inset:
        return outer
    sides = []  # each side moved in, as a point on it and its direction
    for corner in range(6):
        (x1, y1), (x2, y2) = outer[corner], outer[(corner + 1) % 6]
        along_x, along_y = x2 - x1, y2 - y1
        length = math.hypot(along_x, along_y)
        normal_x, normal_y = -along_y / length, along_x / length
        if (center_x - x1) * normal_x + (center_y - y1) * normal_y < 0:
            normal_x, normal_y = -normal_x, -normal_y
        if (center_x - x1) * normal_x + (center_y - y1) * normal_y <= inset:
            return []  # the inset reaches the middle
        sides.append((x1 + normal_x * inset, y1 + normal_y * inset, along_x, along_y))
    corners = []
    for index in range(6):
        ax, ay, ux, uy = sides[index - 1]
        bx, by, vx, vy = sides[index]
        across = ux * vy - uy * vx
        reach = ((bx - ax) * vy - (by - ay) * vx) / across  # where the two moved sides meet, along the first
        corners.append((ax + ux * reach, ay + uy * reach))
    return corners


def through(picture: Image.Image, mask: Image.Image) -> Image.Image:
    """The picture showing only where the mask lets it."""
    out = picture.copy()
    out.putalpha(ImageChops.multiply(picture.getchannel("A"), mask))
    return out


def _solid(size: tuple[int, int], color: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", size, color)


# ---- one draw ----

class _Draw:
    """Everything one draw needs: the design, what came with it, and the clock."""

    def __init__(self, design: dict, inputs: Inputs, deadline: float) -> None:
        self.design = design
        self.inputs = inputs
        self.deadline = deadline
        self.notes: list[str] = []
        self.sizes: dict[str, int] = {}
        self._avatar: Image.Image | None = None
        self._avatar_color: tuple[int, int, int] | None = None

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise _OutOfTime

    def note(self, sentence: str) -> None:
        if sentence not in self.notes and len(self.notes) < 12:
            self.notes.append(sentence)

    @property
    def avatar(self) -> Image.Image:
        if self._avatar is None:
            self._avatar = _still(self.inputs.avatar)
            if self._avatar is None:
                if self.inputs.avatar is not None and self.inputs.avatar.data:
                    self.note("The avatar couldn't be read, so a stand-in is drawn.")
                self._avatar = stand_in_avatar(self.inputs.person)
        return self._avatar

    @property
    def avatar_color(self) -> tuple[int, int, int]:
        if self._avatar_color is None:
            self._avatar_color = dominant_color(self.avatar)
        return self._avatar_color

    def upload(self, upload: str) -> Raw | None:
        return self.inputs.pictures.get(f"upload:{upload}")

    def fill(self, fill: dict, width: int, height: int) -> Image.Image | None:
        """A fill as a picture of this size, or None for no fill."""
        kind = fill["kind"]
        if kind == "none":
            return None
        if kind == "color":
            return _solid((width, height), rgba(fill["color"]))
        if kind == "avatar":
            return _solid((width, height), (*self.avatar_color, 255))
        if kind == "gradient":
            return backgrounds.gradient(width, height, fill["colors"], fill["angle"])
        if kind == "stripes":
            return backgrounds.stripes(width, height, fill["colors"], fill["direction"])
        if kind == "stock":
            key = ("stock", fill["name"], width, height)
            found = CACHE.get(key)
            if found is None:
                drawn = backgrounds.draw(fill["name"], width, height)
                if drawn is None:
                    self.note("A stock background this design names is gone, so the avatar's color stands in.")
                    return _solid((width, height), (*self.avatar_color, 255))
                found = drawn.convert("RGBA")
                CACHE.put(key, found, _bytes_of(found))
            return found
        raw = self.upload(fill["id"])
        picture = _still(raw)
        if picture is None:
            self.note("An uploaded picture this design names is gone, so the avatar's color stands in.")
            return _solid((width, height), (*self.avatar_color, 255))
        key = ("cover", raw.key, width, height)
        found = CACHE.get(key) if raw.key else None
        if found is None:
            found = _cover(picture, width, height)
            if raw.key:
                CACHE.put(key, found, _bytes_of(found))
        return found

    # ---- layers ----

    def shape(self, layer: dict) -> tuple[Image.Image, int]:
        """A rect or an ellipse: its fill, and its stroke inside the edge."""
        width, height = layer["w"], layer["h"]
        kind = "ellipse" if layer["type"] == "ellipse" else "rect"
        outer = shape_mask(kind, width, height, layer.get("radius"))
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        filled = self.fill(layer["fill"], width, height)
        if filled is not None:
            sheet.alpha_composite(through(filled, outer))
        self._stroke(sheet, layer, outer, self._inner(layer, kind))
        return sheet, 0

    @staticmethod
    def _inner(layer: dict, kind: str) -> Image.Image | None:
        """The shape pulled in by the stroke's width, or None when the layer has no stroke to draw."""
        stroke = layer["stroke"]
        if not stroke["width"] or not rgba(stroke["color"])[3]:
            return None
        return shape_mask(kind, layer["w"], layer["h"], layer.get("radius"), stroke["width"])

    @staticmethod
    def _stroke(sheet: Image.Image, layer: dict, outer: Image.Image, inner: Image.Image | None) -> None:
        if inner is not None:
            sheet.alpha_composite(through(_solid(sheet.size, rgba(layer["stroke"]["color"])), ImageChops.subtract(outer, inner)))

    def framed(self, layer: dict, picture: Image.Image | None, kind: str) -> tuple[Image.Image, int]:
        """A picture or an avatar: the fill behind it, the picture inside the stroke, and the stroke around it."""
        width, height = layer["w"], layer["h"]
        outer = shape_mask(kind, width, height, layer.get("radius"))
        inner = self._inner(layer, kind)
        border = layer["stroke"]["width"] if inner is not None else 0
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        filled = self.fill(layer["fill"], width, height)
        if filled is not None:
            sheet.alpha_composite(through(filled, outer))
        inside_w, inside_h = width - 2 * border, height - 2 * border
        if picture is not None and inside_w >= 1 and inside_h >= 1:
            fit = layer.get("fit", "cover")
            if fit == "contain":
                placed = _contain(picture, inside_w, inside_h)
            elif fit == "stretch":
                placed = picture.resize((inside_w, inside_h), Image.Resampling.LANCZOS)
            else:
                placed = _cover(picture, inside_w, inside_h)
            whole = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            whole.alpha_composite(placed, (border, border))
            sheet.alpha_composite(through(whole, inner if inner is not None else outer))
        self._stroke(sheet, layer, outer, inner)
        return sheet, 0

    def picture(self, layer: dict) -> tuple[Image.Image, int]:
        source = layer["source"]
        found: Image.Image | None
        if source["kind"] == "address":
            found = _still(self.inputs.pictures.get(f"address:{layer['id']}"))
            if found is None and source["address"].strip():
                self.note(f"{_called(layer)} has no picture to show. Its address must give a PNG, JPG or GIF on Discord's own image servers, such as {{user(avatar)}} or {{server(icon)}}.")
        elif source["kind"] == "stock":
            drawn = self.fill(source, layer["w"], layer["h"])
            found = drawn
        else:
            found = _still(self.upload(source["id"]))
            if found is None:
                self.note(f"{_called(layer)}'s uploaded picture is gone.")
        return self.framed(layer, found, "rect")

    def avatar_layer(self, layer: dict) -> tuple[Image.Image, int]:
        kind = {"circle": "ellipse", "hexagon": "hexagon"}.get(layer["shape"], "rect")
        shaped = dict(layer)
        if layer["shape"] == "square":
            shaped["radius"] = [0, 0, 0, 0]
        elif layer["shape"] == "rounded" and not any(layer["radius"]):
            shaped["radius"] = [round(min(layer["w"], layer["h"]) * 0.16)] * 4
        return self.framed(shaped, self.avatar, kind)

    def text(self, layer: dict) -> tuple[Image.Image, int]:
        width, height = layer["w"], layer["h"]
        marked = self.inputs.texts.get(layer["id"])
        if marked is None:
            marked = texts.Marked.of(layer["text"])
        stroke = layer["stroke"]
        outline_color = rgba(stroke["color"])
        outline = stroke["width"] if outline_color[3] else 0
        setting = texts.Setting(layer["font"], layer["weight"], layer["size"], layer["line_height"], layer["letter_spacing"], layer["align"], layer["valign"], layer["wrap"], layer["shrink"], outline)
        found = texts.layout(marked, setting, width, height)
        self.sizes[layer["id"]] = found.size
        if found.cut:
            self.note(f"{_called(layer)} holds more text than its box has room for, so the end is cut.")
        pad = outline + round(found.size * 0.4) + 2
        ink, outline_ink, color = texts.paint(found, setting, width, height, pad, self._emoji, self.check)
        sheet = Image.new("RGBA", ink.size, (0, 0, 0, 0))
        if outline_ink is not None:
            sheet.alpha_composite(through(_solid(ink.size, outline_color), outline_ink))
        filled = self.fill(layer["fill"], *ink.size)  # the size of the box and the room around it, so a letter that reaches past the box keeps its fill
        if filled is not None:
            sheet.alpha_composite(through(filled, ink))
        if color is not None:
            sheet.alpha_composite(color)
        return sheet, pad

    def _emoji(self, emoji: str) -> Image.Image | None:
        return _still(self.inputs.pictures.get(f"emoji:{emoji}"))

    def layer(self, layer: dict) -> tuple[Image.Image, int]:
        if layer["type"] == "text":
            return self.text(layer)
        if layer["type"] == "avatar":
            return self.avatar_layer(layer)
        if layer["type"] == "picture":
            return self.picture(layer)
        return self.shape(layer)

    def overlay(self) -> Image.Image:
        """Every layer on a clear sheet the size of the canvas."""
        sheet = Image.new("RGBA", (self.design["width"], self.design["height"]), (0, 0, 0, 0))
        for layer in self.design["layers"]:
            if layer["hidden"] or not layer["opacity"]:
                continue
            self.check()
            drawn, pad = self.layer(layer)
            place(sheet, drawn, layer, pad)
        return sheet

    # ---- the background ----

    def moving(self) -> tuple[list[Image.Image], list[int], int | None] | None:
        """The background's frames fitted to the canvas, how long each shows in milliseconds, and how often it loops. None when it stands still."""
        fill = self.design["background"]
        if fill["kind"] != "upload" or not self.inputs.animate:
            return None
        raw = self.upload(fill["id"])
        if raw is None or not raw.data:
            return None
        width, height = self.design["width"], self.design["height"]
        most = min(MAX_FRAMES, MAX_FRAME_PIXELS // (width * height))
        if most < 2:
            return None
        key = ("frames", raw.key, width, height, most)
        found = CACHE.get(key) if raw.key else None
        if found is not None:
            return found
        try:
            with _open(raw.data) as image:
                if image.format == "GIF":  # laid the way a browser lays them, which is how Discord shows the GIF
                    parsed = gif.parse(raw.data, MAX_SOURCE_FRAMES)
                    count, loop = len(parsed.frames), parsed.loop
                    shown = ((frame.length, canvas) for frame, canvas in gif.composed(parsed, self.check))
                else:
                    count, loop = min(getattr(image, "n_frames", 1), MAX_SOURCE_FRAMES), image.info.get("loop")
                    shown = _seeked(image, count, self.check)
                if count < 2:
                    return None
                kept = min(count, most)
                frames: list[Image.Image] = []
                lengths: list[int] = []
                for index, (length, frame) in enumerate(shown):
                    length = 100 if length < SHORTEST_FRAME else int(length)
                    slot = index * kept // count  # frames past the limit are folded into the ones kept, and their time goes with them
                    if slot == len(frames):
                        backed = Image.new("RGBA", frame.size, (0, 0, 0, 255))
                        backed.alpha_composite(frame if frame.mode == "RGBA" else frame.convert("RGBA"))
                        # Kept at one byte a pixel, so the most frames the limits allow fit in the cache. A GIF frame has 256 colors at most,
                        # and the picture that goes out has 256 in all, so the fitted frame loses next to nothing.
                        frames.append(_cover(backed.convert("RGB"), width, height).quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE))
                        lengths.append(length)
                    else:
                        lengths[-1] += length
        except (Invalid, *images.READ_ERRORS):
            return None
        if kept < count:
            self.note(f"The background has {count:,} frames. This picture's size leaves room for {kept}, so some are skipped and the pace stays the same.")
        result = (frames, lengths, loop if isinstance(loop, int) else None)
        if raw.key:
            CACHE.put(key, result, sum(_bytes_of(frame) for frame in frames))
        return result

    def framed_canvas(self, canvas: Image.Image) -> Image.Image:
        """The finished canvas with its frame around it. The frame grows the picture outward on every side."""
        frame = self.design["frame"]
        color = rgba(frame["color"])
        border = frame["width"]
        if not border:
            return canvas
        out = Image.new("RGBA", (canvas.width + 2 * border, canvas.height + 2 * border), color)
        out.paste(canvas, (border, border))
        return out


def _called(layer: dict) -> str:
    return f"The layer {layer['name']}" if layer["name"] else f"A {layer['type']} layer"


def place(sheet: Image.Image, drawn: Image.Image, layer: dict, pad: int = 0) -> None:
    """Puts a drawn layer on the sheet: flipped, turned about its center, faded, and cut off where it leaves the canvas."""
    if layer["flip_x"]:
        drawn = drawn.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if layer["flip_y"]:
        drawn = drawn.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    turn = layer["rotation"] % 360
    center_x, center_y = layer["x"] + layer["w"] / 2, layer["y"] + layer["h"] / 2
    if turn in (90, 180, 270):
        drawn = drawn.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}[turn])
    elif turn:
        drawn = drawn.rotate(-turn, resample=Image.Resampling.BICUBIC, expand=True)
    if layer["opacity"] < 100:
        drawn.putalpha(drawn.getchannel("A").point([level * layer["opacity"] // 100 for level in range(256)]))
    texts.stamp(sheet, drawn, round(center_x - drawn.width / 2), round(center_y - drawn.height / 2))


# ---- writing the file ----

def _png(image: Image.Image) -> bytes:
    if image.mode == "RGBA" and image.getextrema()[3][0] == 255:
        image = image.convert("RGB")  # nothing shows through, so the file doesn't carry an alpha channel
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def _still_file(image: Image.Image, limit: int) -> tuple[bytes, Image.Image]:
    """A PNG within the byte limit, scaled down when the full size is too heavy. Raises RenderError when even a small one is."""
    data = _png(image)
    for share in (0.75, 0.5, 0.35):
        if len(data) <= limit:
            break
        smaller = image.resize((max(1, round(image.width * share)), max(1, round(image.height * share))), Image.Resampling.LANCZOS)
        data = _png(smaller)
        if len(data) <= limit:
            return data, smaller
    if len(data) > limit:
        raise RenderError("The picture is too heavy to send even at a third of its size. Use a plainer background.")
    return data, image


def _palette(frames: list[Image.Image]) -> Image.Image:
    """One set of 256 colors for a whole animation, from a sheet of every frame in miniature. With one palette, what stands still looks the same in every frame."""
    scale = min(1.0, 160 / frames[0].width, 160 / frames[0].height)
    wide, tall = max(1, round(frames[0].width * scale)), max(1, round(frames[0].height * scale))
    across = 8
    sheet = Image.new("RGB", (wide * across, tall * ((len(frames) + across - 1) // across)))
    for index, frame in enumerate(frames):
        sheet.paste(frame.resize((wide, tall), Image.Resampling.BILINEAR), ((index % across) * wide, (index // across) * tall))
    return sheet.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def _gif(frames: list[Image.Image], lengths: list[int], loop: int | None) -> tuple[bytes, int] | None:
    """The frames as one GIF file, and how many frames it holds. None when every frame comes out the same, since then nothing moves.

    A frame that comes out the same as the one before it, once both have the file's 256 colors, is left out and its time goes to the one
    before. The GIF writer would merge the two itself and add their times past what a GIF can hold, so this does it first, with each
    frame's time capped at a minute."""
    palette = _palette(frames)
    mapped: list[Image.Image] = []
    times: list[int] = []
    for frame, length in zip(frames, lengths):
        image = frame.quantize(palette=palette, dither=Image.Dither.NONE)
        if mapped and image.tobytes() == mapped[-1].tobytes():
            times[-1] = min(LONGEST_FRAME, times[-1] + length)
            continue
        mapped.append(image)
        times.append(min(LONGEST_FRAME, length))
    if len(mapped) < 2:
        return None
    out = io.BytesIO()
    extra: dict[str, Any] = {} if loop is None else {"loop": loop}
    mapped[0].save(out, format="GIF", save_all=True, append_images=mapped[1:], duration=times, disposal=1, optimize=False, **extra)
    return out.getvalue(), len(mapped)


def _halved(frames: list[Image.Image], lengths: list[int]) -> tuple[list[Image.Image], list[int]]:
    """Every other frame, each shown for its own time and its dropped neighbor's."""
    kept = frames[::2]
    times = [lengths[index] + (lengths[index + 1] if index + 1 < len(lengths) else 0) for index in range(0, len(lengths), 2)]
    return kept, times


def render(design: dict, inputs: Inputs) -> Picture:
    """The design as a picture file. Raises RenderError when no picture could be made in the time and size allowed, and nothing else: what
    the drawing libraries refuse comes out as a sentence too, so a join's welcome and the page's preview never meet a bare error."""
    try:
        return _render(design, inputs)
    except (ValueError, OverflowError, MemoryError, RuntimeError, Image.DecompressionBombError) as error:
        log.info("A welcome picture's design couldn't be drawn", exc_info=True)
        raise RenderError(UNDRAWABLE) from error


def _render(design: dict, inputs: Inputs) -> Picture:
    started = time.monotonic()
    most = inputs.seconds if inputs.seconds is not None else float("inf")
    limit = started + most
    # Reading an animation stops early enough to leave a grace under the caller's limit, so a still picture can be drawn when the read runs long.
    work = _Draw(design, inputs, started + min(ANIMATED_SECONDS, most - GRACE_SECONDS))
    width, height = design["width"], design["height"]
    slow_background = False
    try:
        moving = work.moving()
    except _OutOfTime:
        moving = None
        slow_background = True
    overlay = None
    try:
        if moving is not None:
            # The frames are in hand, read or kept from an earlier draw. The animation has the caller's time but what writing a still needs.
            # The layers are drawn once, for the animation and for the still made from its first frame, so they have the later of the
            # animation's time and a still picture's. Only putting the frames together and writing them is held to the animation's.
            animation_deadline = min(started + ANIMATED_SECONDS, limit - STILL_WRITE_SECONDS)
            work.deadline = max(animation_deadline, started + min(STILL_SECONDS, most))
            overlay = work.overlay()
            work.deadline = animation_deadline
        else:
            work.deadline = started + min(STILL_SECONDS, most)
            if slow_background:
                # Reading the frames used the time a still picture has, so the still gets a grace of its own, inside the caller's limit.
                work.note("The background's animation took too long to read, so the picture is a still one.")
                work.deadline = max(work.deadline, min(time.monotonic() + GRACE_SECONDS, limit))
            overlay = work.overlay()
            backdrop = work.fill(design["background"], width, height)
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            if backdrop is not None:
                canvas.alpha_composite(backdrop)
            canvas.alpha_composite(overlay)
            data, final = _still_file(work.framed_canvas(canvas), inputs.max_bytes)
            if final.size != (canvas.width + 2 * design["frame"]["width"], canvas.height + 2 * design["frame"]["width"]):
                work.note("The picture was scaled down to stay under the size limit for one file.")
            return Picture(data, "png", final.width, final.height, 1, work.notes, work.sizes)
    except _OutOfTime:
        if slow_background:
            raise RenderError(SLOW_BACKGROUND) from None
        raise RenderError("The picture took too long to draw. Use fewer or smaller layers.") from None

    frames, lengths, loop = moving
    first = _composed(work, frames[0], overlay)
    try:
        composed = [first]
        for frame in frames[1:]:
            work.check()
            composed.append(_composed(work, frame, overlay))
        lengths = list(lengths)
        scale = 1.0
        while True:
            work.check()
            try:
                written = _gif(composed, lengths, loop)
            except (OverflowError, ValueError):
                work.note("The animation couldn't be written, so the picture is a still one.")
                break
            if written is None:
                work.note("The background's frames look the same once it is fitted to the picture, so the picture is a still one.")
                break
            data, held = written
            if len(data) <= inputs.max_bytes:
                if len(composed) < len(frames):
                    work.note(f"The animation was thinned to {len(composed)} frames to stay under the size limit for one file.")
                if scale < 1.0:
                    work.note("The animation was scaled down to stay under the size limit for one file.")
                return Picture(data, "gif", composed[0].width, composed[0].height, held, work.notes, work.sizes)
            if len(composed) > 12:
                composed, lengths = _halved(composed, lengths)
            elif scale > 0.4:
                scale *= 0.75
                composed = [frame.resize((max(1, round(first.width * scale)), max(1, round(first.height * scale))), Image.Resampling.LANCZOS) for frame in composed]
            else:
                work.note("The animation is too heavy to send, so the picture is a still one.")
                break
    except _OutOfTime:
        work.note("The animation took too long to draw, so the picture is a still one.")
    if time.monotonic() > min(limit, work.deadline + GRACE_SECONDS):
        raise RenderError("The picture took too long to draw. Use a shorter or smaller animation.")
    data, final = _still_file(first.convert("RGBA"), inputs.max_bytes)
    if final.size != first.size:
        work.note("The picture was scaled down to stay under the size limit for one file.")
    return Picture(data, "png", final.width, final.height, 1, work.notes, work.sizes)


def _composed(work: _Draw, frame: Image.Image, overlay: Image.Image) -> Image.Image:
    """One frame of an animation: the background's frame, the layers over it, and the picture's frame around both. Animations are never see-through."""
    canvas = frame.convert("RGBA")
    canvas.alpha_composite(overlay)
    framed = work.framed_canvas(canvas)
    if framed is canvas:
        return canvas.convert("RGB")
    backed = Image.new("RGBA", framed.size, (0, 0, 0, 255))
    backed.alpha_composite(framed)
    return backed.convert("RGB")
