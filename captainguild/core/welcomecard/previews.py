"""Small pictures for the page's pickers: each layout drawn with sample text, and each font writing a few words.

Both come from the same renderer and text code as a real welcome picture, so a picker shows what the choice gives. They are drawn once and
kept, since nothing about them changes while the bot runs. Call them through asyncio.to_thread.
"""
from __future__ import annotations

import functools
import io

from PIL import Image

from . import fonts, layouts, render, text

LAYOUT_WIDTH = 320
SAMPLE_TEXT = "Welcome **Sam**\nto the server!"
SAMPLE_PERSON = 3 << 22
FONT_SAMPLE = (300, 56)
FONT_WORDS = "Welcome aboard 123"
CHIP = (35, 40, 44, 255)  # the dashboard's darkest surface, so a sample reads the same in its light theme


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


@functools.lru_cache(maxsize=32)
def layout_thumbnail(key: str) -> bytes | None:
    """A layout drawn with the starting settings and a sample member, scaled down. None for a layout that doesn't exist."""
    if key not in layouts.LAYOUTS:
        return None
    design = layouts.build({**layouts.DEFAULTS, "layout": key, "text": SAMPLE_TEXT})
    picture = render.render(design, render.Inputs(person=SAMPLE_PERSON))
    with Image.open(io.BytesIO(picture.data)) as image:
        scaled = image.convert("RGB").resize((LAYOUT_WIDTH, max(1, round(image.height * LAYOUT_WIDTH / image.width))), Image.Resampling.LANCZOS)
    return _png(scaled)


@functools.lru_cache(maxsize=32)
def font_sample(key: str) -> bytes | None:
    """A few words in a family at the weight the layouts use, white on a dark chip. None for a family that doesn't exist."""
    family = fonts.FAMILIES.get(key)
    if family is None:
        return None
    width, height = FONT_SAMPLE
    setting = text.Setting(family=family.key, weight=family.display, size=30, align="left", valign="middle", wrap=False, shrink=True)
    pad = 12
    found = text.layout(text.Marked.of(FONT_WORDS, marks=False), setting, width - 2 * pad, height - 2 * pad)
    ink, _, _ = text.paint(found, setting, width - 2 * pad, height - 2 * pad, pad, lambda emoji: None)
    chip = Image.new("RGBA", (width, height), CHIP)
    words = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    words.putalpha(ink)
    chip.alpha_composite(words)
    return _png(chip.convert("RGB"))
