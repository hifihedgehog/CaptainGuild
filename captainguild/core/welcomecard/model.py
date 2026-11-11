"""A welcome picture as plain data: a canvas, a frame around it, and layers drawn from the bottom up.

Everything a server can save about a picture passes through `clean_design`, which returns the design in one fixed shape or raises Invalid with
a sentence that names the limit. The renderer only ever sees a design in that shape, so it needs no checks of its own for what a field holds.

A fill is one of:

    {"kind": "none"}
    {"kind": "color", "color": "#rrggbb" or "#rrggbbaa"}
    {"kind": "avatar"}                                    the member's avatar's color
    {"kind": "gradient", "angle": 0-360, "colors": [...]}  2 to 24 colors, evenly spaced
    {"kind": "stripes", "direction": "horizontal" or "vertical", "colors": [...]}
    {"kind": "stock", "name": "..."}                      a background drawn by backgrounds.py
    {"kind": "upload", "id": "..."}                       a picture the server uploaded

A layer is a text, a picture, an avatar, a rect or an ellipse. Every layer has a box (x, y, w, h), a rotation in degrees clockwise, two flips,
an opacity from 0 to 100, a fill and a stroke. The stroke sits inside the box. On a text it is the outline around the letters, and on an
avatar it is the ring.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

from ..validate import Invalid

VERSION = 1
MIN_SIDE = 100
MAX_SIDE = 2000
MAX_LAYERS = 40
MAX_TEXT = 1000           # characters of template in one text layer
MAX_ADDRESS = 300         # characters of template in a picture's address
MAX_NAME = 60             # characters in a layer's name
MAX_COLORS = 24           # colors in one striped or gradient fill
MAX_STROKE = 64           # pixels of stroke, outline, ring, or frame around the canvas
MAX_RADIUS = 1000
MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 400
MAX_OFFSET = 4000         # how far outside the canvas a layer's corner can sit
MAX_LAYER_PIXELS = 24_000_000  # the boxes of all layers together, which is what a draw costs
TYPES = ("text", "picture", "avatar", "rect", "ellipse")
FILL_KINDS = ("none", "color", "avatar", "gradient", "stripes", "stock", "upload")
SHAPES = ("circle", "rounded", "square", "hexagon")
FITS = ("cover", "contain", "stretch")
ALIGNS = ("left", "center", "right")
VALIGNS = ("top", "middle", "bottom")
DIRECTIONS = ("horizontal", "vertical")
SOURCE_KINDS = ("upload", "address", "stock")

_HEX = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(\d{1,3}(?:\.\d+)?|\.\d+)\s*)?\)")
_ID = re.compile(r"[a-z0-9_-]{1,16}")
_KEY = re.compile(r"[a-z0-9_-]{1,40}")
BAD_COLOR = "That isn't a color. Use six hex digits such as #ff8800, or eight to set how solid it is."


def color(value: Any) -> str:
    """A color as "#rrggbb", or "#rrggbbaa" when it is see-through. Takes hex with three, six or eight digits, a number, and rgb() or rgba()."""
    if isinstance(value, bool):
        raise Invalid(BAD_COLOR)
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFF:
            raise Invalid(BAD_COLOR)
        return f"#{value:06x}"
    if not isinstance(value, str):
        raise Invalid(BAD_COLOR)
    text = value.strip()
    match = _HEX.fullmatch(text)
    if match:
        digits = match.group(1).lower()
        if len(digits) == 3:
            digits = "".join(digit * 2 for digit in digits)
        return "#" + (digits[:6] if digits[6:] == "ff" else digits)
    match = _RGB.fullmatch(text.lower())
    if match:
        red, green, blue = (int(match.group(index)) for index in (1, 2, 3))
        if max(red, green, blue) > 255:
            raise Invalid(BAD_COLOR)
        alpha = 255
        if match.group(4) is not None:
            amount = float(match.group(4))
            alpha = round(amount if amount > 1 else amount * 255)  # 0 to 1 is a share, and anything above is out of 255
            if alpha > 255:
                raise Invalid(BAD_COLOR)
        return f"#{red:02x}{green:02x}{blue:02x}" + ("" if alpha == 255 else f"{alpha:02x}")
    raise Invalid(BAD_COLOR)


def rgba(text: str) -> tuple[int, int, int, int]:
    """A cleaned color as four numbers from 0 to 255."""
    digits = text.lstrip("#")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), int(digits[6:8], 16) if len(digits) == 8 else 255


def _whole(value: Any, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise Invalid(f"{what} must be a number.")
    number = round(value)
    if not low <= number <= high:
        raise Invalid(f"{what} can be {low:,} to {high:,}. This is {number:,}.")
    return number


def _decimal(value: Any, low: float, high: float, what: str, places: int = 2) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise Invalid(f"{what} must be a number.")
    if not low <= value <= high:
        raise Invalid(f"{what} can be {low:g} to {high:g}. This is {value:g}.")
    number = round(float(value), places)
    return int(number) if number == int(number) else number


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise Invalid(f"{what} must be on or off.")
    return value


def _choice(value: Any, choices: tuple[str, ...], what: str) -> str:
    if value not in choices:
        raise Invalid(f"{what} must be one of: {', '.join(choices)}.")
    return value


def _words(value: Any, limit: int, what: str, *, strip: bool = True) -> str:
    if not isinstance(value, str):
        raise Invalid(f"{what} must be text.")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip() if strip else text
    if len(text) > limit:
        raise Invalid(f"{what} can be at most {limit:,} characters. This is {len(text):,}.")
    return text


def _only(data: dict, allowed: tuple[str, ...], what: str) -> None:
    extra = sorted(str(name) for name in data if name not in allowed)
    if extra:
        raise Invalid(f"{what} has no setting called {extra[0]}.")


def _colors(value: Any, least: int, what: str) -> list[str]:
    if not isinstance(value, list):
        raise Invalid(f"{what} need a list of colors.")
    if len(value) < least:
        raise Invalid(f"{what} need at least {least} {'color' if least == 1 else 'colors'}.")
    if len(value) > MAX_COLORS:
        raise Invalid(f"{what} can hold at most {MAX_COLORS} colors. This has {len(value)}.")
    return [color(item) for item in value]


def clean_fill(value: Any, what: str = "The fill") -> dict:
    """A fill in its fixed shape. Raises Invalid."""
    if not isinstance(value, dict):
        raise Invalid(f"{what} must be a set of fields.")
    kind = _choice(value.get("kind"), FILL_KINDS, f"{what}'s kind")
    if kind == "color":
        _only(value, ("kind", "color"), what)
        return {"kind": kind, "color": color(value.get("color", "#000000"))}
    if kind == "gradient":
        _only(value, ("kind", "angle", "colors"), what)
        return {"kind": kind, "angle": _whole(value.get("angle", 90), 0, 360, f"{what}'s angle"), "colors": _colors(value.get("colors"), 2, "Gradients")}
    if kind == "stripes":
        _only(value, ("kind", "direction", "colors"), what)
        return {"kind": kind, "direction": _choice(value.get("direction", "horizontal"), DIRECTIONS, f"{what}'s direction"), "colors": _colors(value.get("colors"), 1, "Stripes")}
    if kind == "stock":
        _only(value, ("kind", "name"), what)
        name = value.get("name")
        if not isinstance(name, str) or not _KEY.fullmatch(name):
            raise Invalid(f"{what} names no stock background.")
        return {"kind": kind, "name": name}
    if kind == "upload":
        _only(value, ("kind", "id"), what)
        upload = value.get("id")
        if not isinstance(upload, str) or not _KEY.fullmatch(upload):
            raise Invalid(f"{what} names no uploaded picture.")
        return {"kind": kind, "id": upload}
    _only(value, ("kind",), what)
    return {"kind": kind}


def clean_stroke(value: Any, what: str, default_color: str = "#000000") -> dict:
    if value is None:
        return {"color": default_color, "width": 0}
    if not isinstance(value, dict):
        raise Invalid(f"{what} must be a set of fields.")
    _only(value, ("color", "width"), what)
    return {"color": color(value.get("color", default_color)), "width": _whole(value.get("width", 0), 0, MAX_STROKE, f"{what}'s width")}


def _radius(value: Any, what: str) -> list[int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value] * 4
    if not isinstance(value, list) or len(value) != 4:
        raise Invalid(f"{what} need four numbers: top left, top right, bottom right, bottom left.")
    return [_whole(item, 0, MAX_RADIUS, what) for item in value]


def _source(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise Invalid(f"{what} needs a picture: an upload, a stock background or an address.")
    kind = _choice(value.get("kind"), SOURCE_KINDS, f"{what}'s kind")
    if kind == "address":
        _only(value, ("kind", "address"), what)
        return {"kind": kind, "address": _words(value.get("address", ""), MAX_ADDRESS, f"{what}'s address")}
    return clean_fill(value, what)


_COMMON = ("id", "type", "name", "hidden", "x", "y", "w", "h", "rotation", "flip_x", "flip_y", "opacity", "fill", "stroke")
_EXTRA = {
    "text": ("text", "font", "weight", "size", "line_height", "letter_spacing", "align", "valign", "wrap", "shrink"),
    "picture": ("radius", "source", "fit"),
    "avatar": ("radius", "shape"),
    "rect": ("radius",),
    "ellipse": (),
}
_DEFAULT_FILL = {
    "text": {"kind": "color", "color": "#ffffff"},
    "picture": {"kind": "none"},
    "avatar": {"kind": "color", "color": "#48525c"},
    "rect": {"kind": "color", "color": "#00a2f8"},
    "ellipse": {"kind": "color", "color": "#00a2f8"},
}


def clean_layer(value: Any, number: int, fonts: tuple[str, ...]) -> dict:
    """One layer in its fixed shape. `number` counts from 1, for the sentence a refusal gives."""
    what = f"Layer {number}"
    if not isinstance(value, dict):
        raise Invalid(f"{what} must be a set of fields.")
    kind = _choice(value.get("type"), TYPES, f"{what}'s type")
    _only(value, _COMMON + _EXTRA[kind], what)
    name = _words(value.get("name", ""), MAX_NAME, f"{what}'s name")
    if name:
        what = f"Layer {number} ({name})"
    identity = value.get("id", "")
    if identity != "" and (not isinstance(identity, str) or not _ID.fullmatch(identity)):
        raise Invalid(f"{what}'s ID can hold up to 16 small letters, digits, dashes and underscores.")
    out: dict[str, Any] = {
        "id": identity,
        "type": kind,
        "name": name,
        "hidden": _flag(value.get("hidden", False), f"{what}'s hidden switch"),
        "x": _whole(value.get("x", 0), -MAX_OFFSET, MAX_OFFSET, f"{what}'s left edge"),
        "y": _whole(value.get("y", 0), -MAX_OFFSET, MAX_OFFSET, f"{what}'s top edge"),
        "w": _whole(value.get("w", 200), 1, MAX_SIDE, f"{what}'s width"),
        "h": _whole(value.get("h", 100), 1, MAX_SIDE, f"{what}'s height"),
        "rotation": _decimal(value.get("rotation", 0), -360, 360, f"{what}'s rotation"),
        "flip_x": _flag(value.get("flip_x", False), f"{what}'s flip"),
        "flip_y": _flag(value.get("flip_y", False), f"{what}'s flip"),
        "opacity": _whole(value.get("opacity", 100), 0, 100, f"{what}'s opacity"),
        "fill": clean_fill(value.get("fill", _DEFAULT_FILL[kind]), f"{what}'s fill"),
        "stroke": clean_stroke(value.get("stroke"), f"{what}'s stroke", "#ffffff" if kind == "avatar" else "#000000"),
    }
    if kind == "text":
        font = value.get("font", fonts[0])
        if font not in fonts:
            raise Invalid(f"{what} names a font this bot doesn't have.")
        out.update({
            "text": _words(value.get("text", ""), MAX_TEXT, f"{what}'s text", strip=False),
            "font": font,
            "weight": _whole(value.get("weight", 400), 100, 900, f"{what}'s weight"),
            "size": _decimal(value.get("size", 48), MIN_FONT_SIZE, MAX_FONT_SIZE, f"{what}'s size", 1),
            "line_height": _decimal(value.get("line_height", 1.2), 0.6, 3, f"{what}'s line height"),
            "letter_spacing": _decimal(value.get("letter_spacing", 0), -20, 100, f"{what}'s letter spacing", 1),
            "align": _choice(value.get("align", "left"), ALIGNS, f"{what}'s alignment"),
            "valign": _choice(value.get("valign", "top"), VALIGNS, f"{what}'s vertical alignment"),
            "wrap": _flag(value.get("wrap", True), f"{what}'s wrap switch"),
            "shrink": _flag(value.get("shrink", True), f"{what}'s shrink switch"),
        })
    if "radius" in _EXTRA[kind]:
        out["radius"] = _radius(value.get("radius", 0), f"{what}'s corners")
    if kind == "picture":
        out["source"] = _source(value.get("source", {"kind": "address", "address": ""}), f"{what}'s picture")
        out["fit"] = _choice(value.get("fit", "cover"), FITS, f"{what}'s fit")
    if kind == "avatar":
        out["shape"] = _choice(value.get("shape", "circle"), SHAPES, f"{what}'s shape")
    return out


def clean_design(value: Any, fonts: tuple[str, ...]) -> dict:
    """A whole design in its fixed shape, with every limit checked. Raises Invalid with a sentence that names the limit it met."""
    if not isinstance(value, dict):
        raise Invalid("A design must be a set of fields.")
    _only(value, ("v", "width", "height", "background", "frame", "layers"), "A design")
    if value.get("v", VERSION) != VERSION:
        raise Invalid("That design was made by a newer version of this bot.")
    for side in ("width", "height"):
        size = value.get(side, 1000 if side == "width" else 300)
        if isinstance(size, bool) or not isinstance(size, (int, float)) or size != size or not MIN_SIDE <= size <= MAX_SIDE:
            shown = f"{size:,.0f}" if isinstance(size, (int, float)) and not isinstance(size, bool) and size == size and abs(size) < 1e12 else "not a size"
            raise Invalid(f"The canvas can be {MIN_SIDE} to {MAX_SIDE:,} pixels on a side. Its {side} is {shown}.")
    layers = value.get("layers", [])
    if not isinstance(layers, list):
        raise Invalid("A design's layers must be a list.")
    if len(layers) > MAX_LAYERS:
        raise Invalid(f"A design can hold at most {MAX_LAYERS} layers. This one has {len(layers)}.")
    cleaned = [clean_layer(layer, index + 1, fonts) for index, layer in enumerate(layers)]
    covered = sum(layer["w"] * layer["h"] for layer in cleaned)
    if covered > MAX_LAYER_PIXELS:
        raise Invalid(f"The layers cover {covered / 1_000_000:.1f} million pixels together. The limit is {MAX_LAYER_PIXELS // 1_000_000} million, so make some smaller or remove one.")
    taken: set[str] = set()
    for layer in cleaned:
        if layer["id"] in taken:
            layer["id"] = ""  # a copied layer keeps its look and gets an ID of its own
        taken.add(layer["id"])
    counter = 1
    for layer in cleaned:
        if not layer["id"]:
            while f"l{counter}" in taken:
                counter += 1
            layer["id"] = f"l{counter}"
            taken.add(layer["id"])
    frame = clean_stroke(value.get("frame"), "The frame", "#ffffff")
    return {
        "v": VERSION,
        "width": round(value.get("width", 1000)),
        "height": round(value.get("height", 300)),
        "background": clean_fill(value.get("background", {"kind": "avatar"}), "The background"),
        "frame": frame,
        "layers": cleaned,
    }


def fills_in(design: dict) -> Iterator[dict]:
    """Every fill and picture source a design draws from, hidden layers left out."""
    yield design["background"]
    for layer in design["layers"]:
        if layer["hidden"]:
            continue
        yield layer["fill"]
        if layer["type"] == "picture":
            yield layer["source"]


def uploads_in(design: dict) -> set[str]:
    """The uploaded pictures a design names, hidden layers included, since showing one again must not break it."""
    found = {design["background"]["id"]} if design["background"]["kind"] == "upload" else set()
    for layer in design["layers"]:
        for part in (layer["fill"], layer.get("source") or {}):
            if part.get("kind") == "upload":
                found.add(part["id"])
    return found


def uses_avatar(design: dict) -> bool:
    """Whether drawing this design needs the member's avatar: for an avatar layer, or for a fill that takes its color."""
    return any(fill["kind"] == "avatar" for fill in fills_in(design)) or any(layer["type"] == "avatar" and not layer["hidden"] for layer in design["layers"])
