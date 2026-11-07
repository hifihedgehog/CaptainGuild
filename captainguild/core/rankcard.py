"""Draws a member's rank card with Pillow. Nothing here touches Discord, so call these through asyncio.to_thread."""
from __future__ import annotations

import functools
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import images

WIDTH, HEIGHT = 934, 282
FONT_FILE = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "DejaVuSans-Bold.ttf"
SCALE = 3  # shapes are drawn large and scaled down, which gives them smooth edges
TOP_COLOR, BOTTOM_COLOR = (34, 58, 79), (16, 20, 26)
TRACK_COLOR = (24, 26, 44)
BLANK_AVATAR = (72, 82, 92)
TEXT_LEFT, TEXT_RIGHT = 303, 862


@functools.lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_FILE), size)


def _rgb(color: int) -> tuple[int, int, int]:
    return (color >> 16) & 255, (color >> 8) & 255, color & 255


def short_number(value: int) -> str:
    """1,234 reads as 1.2k and 2,500,000 as 2.5M, the way the card has room for."""
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(value)


@functools.lru_cache(maxsize=4096)
def _can_draw(character: str) -> bool:
    font = _font(40)
    mask = font.getmask(character)
    return bool(mask.size[0]) and bytes(mask) != bytes(font.getmask("\U0010FFFF"))


def drawable(name: str, fallback: str) -> str:
    """A name without the characters the font lacks, which would draw as empty boxes. Falls back when nothing is left."""
    kept = "".join(c for c in name if c == " " or _can_draw(c)).strip()
    return kept or fallback


def _fit(draw: ImageDraw.ImageDraw, text: str, width: int, largest: int, smallest: int) -> tuple[str, ImageFont.FreeTypeFont]:
    """The largest size that fits, then the text cut short if even the smallest doesn't."""
    for size in range(largest, smallest - 1, -2):
        font = _font(size)
        if draw.textlength(text, font=font) <= width:
            return text, font
    font = _font(smallest)
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "...", font


def _default_background() -> Image.Image:
    mask = Image.linear_gradient("L").resize((WIDTH, HEIGHT))
    return Image.composite(Image.new("RGB", (WIDTH, HEIGHT), BOTTOM_COLOR), Image.new("RGB", (WIDTH, HEIGHT), TOP_COLOR), mask)


def _open(data: bytes) -> Image.Image:
    """A picture in memory. A stored one can come from a restored backup, so its format and size are checked again before it's decoded."""
    with Image.open(io.BytesIO(data), formats=images.OPENED_FORMATS) as image:
        if max(image.size) > images.MAX_SIDE:
            raise ValueError(f"The picture is {image.size[0]}x{image.size[1]} pixels.")
        image.seek(0)
        return image.convert("RGBA")


def prepare_background(data: bytes) -> bytes:
    """Check an image and turn it into a stored background: scaled to the card's width and cropped to its height. Raises Invalid."""
    images.check(data)
    try:
        picture = _open(data)
    except images.READ_ERRORS as error:
        raise images.Invalid("That file isn't an image the bot can read. Use a JPG, PNG or GIF.") from error
    flat = Image.new("RGB", picture.size, BOTTOM_COLOR)
    flat.paste(picture, mask=picture.getchannel("A"))
    fitted = ImageOps.fit(flat, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
    out = io.BytesIO()
    fitted.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _avatar(data: bytes | None, letter: str, side: int) -> Image.Image:
    if data:
        try:
            fitted = ImageOps.fit(_open(data), (side, side), method=Image.Resampling.LANCZOS)
            backed = Image.new("RGBA", (side, side), BLANK_AVATAR)  # a see-through avatar sits on this, not on whatever its hidden pixels hold
            backed.alpha_composite(fitted)
            return backed
        except images.READ_ERRORS:
            pass
    blank = Image.new("RGBA", (side, side), BLANK_AVATAR)
    ImageDraw.Draw(blank).text((side / 2, side / 2), letter, font=_font(side // 2), fill=(255, 255, 255), anchor="mm")
    return blank


def draw(*, name: str, level: int, rank: int | None, into_level: int, level_size: int, avatar: bytes | None = None, background: bytes | None = None,
         color: int = 0x00A2F8, accent: int = 0xFFFFFF, opacity: float = 75.0) -> bytes:
    """The finished card as PNG bytes. `color` is the bar and the avatar ring, `accent` is the text, `opacity` is the dark panel from 0 to 100."""
    card = _default_background()
    if background:
        try:
            card = _open(background).convert("RGB").resize((WIDTH, HEIGHT))
        except images.READ_ERRORS:
            pass
    card = card.convert("RGBA")
    bar_rgb, text_rgb = _rgb(color), _rgb(accent)

    large = (WIDTH * SCALE, HEIGHT * SCALE)
    panel = Image.new("RGBA", large, (0, 0, 0, 0))
    alpha = round(255 * max(0.0, min(100.0, opacity)) / 100)
    ImageDraw.Draw(panel).rounded_rectangle((20 * SCALE, 20 * SCALE, (WIDTH - 20) * SCALE, (HEIGHT - 20) * SCALE), radius=22 * SCALE, fill=(0, 0, 0, alpha))
    card.alpha_composite(panel.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS))

    draw_on = ImageDraw.Draw(card)
    shown = drawable(name, "Member")

    # The avatar sits in a ring of the bar's color.
    box = 204
    left, top = 58, (HEIGHT - box) // 2
    side = box * SCALE
    picture = _avatar(avatar, shown[0].upper(), side)
    circle = Image.new("L", (side, side), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, side - 1, side - 1), fill=255)
    ring = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, side - 1, side - 1), fill=(*bar_rgb, 255))
    inner = Image.new("L", (side, side), 0)
    border = 5 * SCALE
    ImageDraw.Draw(inner).ellipse((border, border, side - 1 - border, side - 1 - border), fill=255)
    ring.paste(picture, mask=inner)
    ring.putalpha(circle)
    card.alpha_composite(ring.resize((box, box), Image.Resampling.LANCZOS), (left, top))

    # Rank on the right, and the name takes the room that leaves.
    rank_text = f"Rank #{rank:,}" if rank else "Unranked"
    rank_font = _font(26)
    rank_width = int(draw_on.textlength(rank_text, font=rank_font))
    draw_on.text((TEXT_RIGHT, 112), rank_text, font=rank_font, fill=text_rgb, anchor="rs")
    name_text, name_font = _fit(draw_on, shown, TEXT_RIGHT - TEXT_LEFT - rank_width - 24, 46, 20)
    draw_on.text((TEXT_LEFT, 112), name_text, font=name_font, fill=text_rgb, anchor="ls")
    draw_on.line((TEXT_LEFT, 128, TEXT_RIGHT, 128), fill=(*text_rgb, 255), width=2)

    draw_on.text((TEXT_LEFT, 176), f"Level {level:,}", font=_font(28), fill=text_rgb, anchor="ls")
    draw_on.text((TEXT_RIGHT, 176), f"{short_number(into_level)}/{short_number(level_size)}", font=_font(21), fill=text_rgb, anchor="rs")

    # The bar: a rounded track, filled as far as this level has come.
    fraction = max(0.0, min(1.0, into_level / level_size)) if level_size else 0.0
    bar_box = (TEXT_LEFT, 190, TEXT_RIGHT, 240)
    bar_width, bar_height = (bar_box[2] - bar_box[0]) * SCALE, (bar_box[3] - bar_box[1]) * SCALE
    track = Image.new("RGBA", (bar_width, bar_height), (*TRACK_COLOR, 255))
    if fraction:
        ImageDraw.Draw(track).rectangle((0, 0, int(bar_width * fraction), bar_height), fill=(*bar_rgb, 255))
    shape = Image.new("L", (bar_width, bar_height), 0)
    ImageDraw.Draw(shape).rounded_rectangle((0, 0, bar_width - 1, bar_height - 1), radius=9 * SCALE, fill=255)
    ImageDraw.Draw(track).rounded_rectangle((0, 0, bar_width - 1, bar_height - 1), radius=9 * SCALE, outline=(*text_rgb, 255), width=2 * SCALE)
    track.putalpha(shape)
    card.alpha_composite(track.resize((bar_box[2] - bar_box[0], bar_box[3] - bar_box[1]), Image.Resampling.LANCZOS), bar_box[:2])
    # A dark edge keeps the number readable over any bar color.
    draw_on.text(((bar_box[0] + bar_box[2]) / 2, (bar_box[1] + bar_box[3]) / 2), f"{int(fraction * 100)}%", font=_font(24), fill=text_rgb, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))

    out = io.BytesIO()
    card.convert("RGB").save(out, format="PNG")
    return out.getvalue()
