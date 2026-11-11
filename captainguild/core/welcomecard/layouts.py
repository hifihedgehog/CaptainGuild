"""The ready-made layouts: each one builds a whole design from a handful of simple settings.

A layout is a function from those settings to layers, so the ready-made pictures are drawn by the same renderer as a design someone made by
hand, and "Customize in the designer" is nothing more than keeping what the function returns.

Wide, Tall and Badge use the canvas sizes and the arrangement that welcome pictures have long had, so a background made for one of those
sizes fits. The others are this bot's own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import fonts
from .model import clean_design, rgba

POSITIONS: dict[str, tuple[str, str]] = {
    "top_left": ("left", "top"), "top": ("center", "top"), "top_right": ("right", "top"),
    "left": ("left", "middle"), "center": ("center", "middle"), "right": ("right", "middle"),
    "bottom_left": ("left", "bottom"), "bottom": ("center", "bottom"), "bottom_right": ("right", "bottom"),
}
AUTO = "auto"  # the text position that follows the layout: each layout has a place where its text sits best
TEXT_POSITIONS: tuple[str, ...] = (AUTO, *POSITIONS)
SHAPES = ("circle", "rounded", "square", "hexagon")
DEFAULT_TEXT = "Welcome **{user}**\nto {server}!\nYou are our {ord:{server(members)}} member."
# What the simple settings start as. The module's schema takes its defaults from here.
DEFAULTS: dict = {
    "layout": "wide",
    "background": {"kind": "stock", "name": "dusk"},
    "text": DEFAULT_TEXT,
    "text_position": AUTO,
    "text_color": "#ffffff",
    "outline_color": "#000000",
    "outline_width": 4,
    "font": fonts.DEFAULT,
    "avatar": True,
    "avatar_shape": "circle",
    "ring_color": "#ffffff",
    "ring_width": 8,
    "frame": False,
    "frame_color": "#ffffff",
    "frame_width": 16,
}


def _faded(color: str, share: float) -> str:
    """A color at a share of its own opacity."""
    red, green, blue, alpha = rgba(color)
    return f"#{red:02x}{green:02x}{blue:02x}{round(alpha * share):02x}"


def _text(cfg: dict, x: int, y: int, w: int, h: int, size: int) -> dict:
    align, valign = POSITIONS[cfg["text_position"]]
    family = fonts.FAMILIES[cfg["font"]]
    return {
        "id": "text", "type": "text", "name": "Text", "x": x, "y": y, "w": w, "h": h, "text": cfg["text"], "font": family.key, "weight": family.display, "size": size,
        "align": align, "valign": valign, "fill": {"kind": "color", "color": cfg["text_color"]}, "stroke": {"color": cfg["outline_color"], "width": cfg["outline_width"]},
    }


def _avatar(cfg: dict, x: int, y: int, side: int) -> dict:
    return {"id": "avatar", "type": "avatar", "name": "Avatar", "x": x, "y": y, "w": side, "h": side, "shape": cfg["avatar_shape"],
            "stroke": {"color": cfg["ring_color"], "width": min(cfg["ring_width"], side // 3)}}


def _box(identity: str, name: str, x: int, y: int, w: int, h: int, color: str, **more: object) -> dict:
    return {"id": identity, "type": "rect", "name": name, "x": x, "y": y, "w": w, "h": h, "fill": {"kind": "color", "color": color}, **more}


def _wide(cfg: dict) -> list[dict]:
    if not cfg["avatar"]:
        return [_text(cfg, 32, 32, 936, 236, 72)]
    return [_avatar(cfg, 32, 32, 236), _text(cfg, 300, 32, 668, 236, 72)]


def _tall(cfg: dict) -> list[dict]:
    if not cfg["avatar"]:
        return [_text(cfg, 32, 32, 686, 452, 72)]
    return [_avatar(cfg, 257, 32, 236), _text(cfg, 32, 276, 686, 208, 64)]


def _badge(cfg: dict) -> list[dict]:
    # The panel is dark glass edged in the ring's color, so the ring and the panel read as one piece and light text stays readable on it.
    edge = {"color": cfg["ring_color"], "width": (cfg["ring_width"] + 1) // 2}
    if not cfg["avatar"]:
        return [_box("panel", "Panel", 32, 65, 900, 190, "#000000a6", radius=32, stroke=edge), _text(cfg, 72, 82, 820, 156, 56)]
    return [_box("panel", "Panel", 82, 65, 850, 190, "#000000a6", radius=32, stroke=edge), _avatar(cfg, 32, 32, 256), _text(cfg, 320, 82, 579, 156, 56)]


def _banner(cfg: dict) -> list[dict]:
    band = _box("band", "Band", 0, 208, 1000, 152, "#00000073")
    if not cfg["avatar"]:
        return [_box("band", "Band", 0, 70, 1000, 220, "#00000073"), _text(cfg, 40, 82, 920, 196, 64)]
    return [band, _avatar(cfg, 420, 28, 160), _text(cfg, 40, 216, 920, 136, 52)]


def _split(cfg: dict) -> list[dict]:
    if not cfg["avatar"]:
        return [_box("accent", "Accent", 40, 60, 10, 280, cfg["ring_color"], radius=5), _text(cfg, 80, 40, 880, 320, 68)]
    return [
        _box("panel", "Panel", -80, -60, 440, 520, "#0000008c", rotation=6),
        _box("accent", "Accent", 357, -60, 8, 520, cfg["ring_color"], rotation=6),
        _avatar(cfg, 62, 90, 220),
        _text(cfg, 412, 40, 548, 320, 60),
    ]


def _strip(cfg: dict) -> list[dict]:
    if not cfg["avatar"]:
        return [_text(cfg, 24, 14, 852, 122, 46)]
    return [_avatar(cfg, 20, 20, 110), _text(cfg, 150, 14, 730, 122, 46)]


def _spotlight(cfg: dict) -> list[dict]:
    if not cfg["avatar"]:
        return [_box("band", "Band", 0, 190, 640, 260, "#00000073"), _text(cfg, 40, 204, 560, 232, 64)]
    rings = [
        {"id": "ring2", "type": "ellipse", "name": "Outer ring", "x": 130, "y": 14, "w": 380, "h": 380, "fill": {"kind": "none"}, "stroke": {"color": _faded(cfg["ring_color"], 0.22), "width": 5}},
        {"id": "ring1", "type": "ellipse", "name": "Inner ring", "x": 160, "y": 44, "w": 320, "h": 320, "fill": {"kind": "none"}, "stroke": {"color": _faded(cfg["ring_color"], 0.45), "width": 5}},
    ]
    return [*rings, _avatar(cfg, 190, 74, 260), _text(cfg, 40, 420, 560, 190, 56)]


@dataclass(frozen=True)
class Layout:
    key: str
    name: str
    width: int
    height: int
    note: str
    layers: Callable[[dict], list[dict]]
    position: str = "left"  # where its text sits when the text position is left to the layout


LAYOUTS: dict[str, Layout] = {layout.key: layout for layout in (
    Layout("wide", "Wide", 1000, 300, "The avatar on the left and the text beside it.", _wide),
    Layout("tall", "Tall", 750, 516, "The avatar on top and the text under it.", _tall, "center"),
    Layout("badge", "Badge", 964, 320, "A rounded panel with the avatar over its left end.", _badge),
    Layout("banner", "Banner", 1000, 360, "The avatar in the middle, over a dark band that holds the text.", _banner, "center"),
    Layout("split", "Split", 1000, 400, "A slanted dark panel for the avatar, and the text on the open side.", _split),
    Layout("strip", "Strip", 900, 150, "A slim picture for a busy channel.", _strip),
    Layout("spotlight", "Spotlight", 640, 640, "A square picture with the avatar ringed in the middle.", _spotlight, "center"),
)}
KEYS: tuple[str, ...] = tuple(LAYOUTS)


def build(cfg: dict) -> dict:
    """The design a layout makes from the simple settings. `cfg` holds every key of DEFAULTS, as the module's settings do. A text position
    of "auto" takes the layout's own, and any of the nine named ones applies as chosen."""
    layout = LAYOUTS.get(cfg["layout"]) or LAYOUTS["wide"]
    if cfg["text_position"] not in POSITIONS:
        cfg = {**cfg, "text_position": layout.position}
    design = {
        "width": layout.width,
        "height": layout.height,
        "background": cfg["background"],
        "frame": {"color": cfg["frame_color"], "width": cfg["frame_width"] if cfg["frame"] else 0},
        "layers": layout.layers(cfg),
    }
    return clean_design(design, fonts.KEYS)


def catalog() -> list[dict]:
    """The layouts for the page's picker."""
    return [{"key": layout.key, "name": layout.name, "width": layout.width, "height": layout.height, "note": layout.note, "position": layout.position} for layout in LAYOUTS.values()]
