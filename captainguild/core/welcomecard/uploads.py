"""Turns a file a server uploads into a stored picture: checked, sized for a canvas, and with a small preview for the page's list.

A still picture is saved again by this code, which sets its turn from the camera's note upright, drops everything else a camera adds, and
scales it down to the largest canvas. One with see-through parts stays a PNG and the rest become JPEGs. An animated GIF is kept as its
reader reads it, and an animated PNG with only the chunks that draw it, so their frames lose nothing before the renderer fits them, and
nothing else the file carried is kept. These run Pillow, so call them through asyncio.to_thread.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from .. import images
from ..validate import Invalid
from . import gif
from .model import MAX_SIDE
from .render import MAX_SOURCE_FRAMES

MAX_ANIMATION_PIXELS = 200_000_000  # frames times pixels in one uploaded GIF, which is what reading it costs
PREVIEW = (240, 135)
JPEG_QUALITY = 90
BACKING = (72, 82, 92)  # what shows through a see-through picture in its preview
UNREADABLE = "That file isn't an image the bot can read. Use a JPG, PNG or GIF."
UNSAVABLE = "The bot couldn't store that picture. Save it again as a JPG or PNG and send that."
CROWDED = "That GIF holds more blocks than the bot reads. Save it again in an image editor and send that."
# The chunks that draw an animated PNG: its size, colors and transparency, how it is shown, its frames and its end. Text, EXIF, color
# profiles, dates and every other note are left out.
PNG_KEPT = frozenset({b"IHDR", b"PLTE", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT", b"acTL", b"fcTL", b"IDAT", b"fdAT", b"IEND"})


@dataclass(frozen=True)
class Prepared:
    data: bytes
    kind: str       # the stored file's type, such as image/png
    width: int
    height: int
    frames: int
    preview: bytes  # a small JPEG


def _preview(picture: Image.Image) -> bytes:
    small = picture.convert("RGBA")
    small.thumbnail(PREVIEW, Image.Resampling.LANCZOS)
    backed = Image.new("RGB", small.size, BACKING)
    backed.paste(small, (0, 0), small.getchannel("A"))
    out = io.BytesIO()
    backed.save(out, format="JPEG", quality=80)
    return out.getvalue()


def prepare(data: bytes) -> Prepared:
    """Check an uploaded file and make the picture to store. Raises Invalid with a sentence for the person who sent it."""
    images.check(data)
    try:
        if data[:6] in gif.SIGNATURES:
            # A GIF is counted, sized and read the way the renderer reads it, which is the way a browser shows it, and only once.
            animated, picture = _gif(data)
            if animated is not None:
                return animated
        else:
            with Image.open(io.BytesIO(data), formats=images.OPENED_FORMATS) as image:
                frames = getattr(image, "n_frames", 1)
                _within_limits(frames, *image.size)
                if frames > 1:
                    image.seek(0)
                    preview = _preview(image)
                    for index in range(1, frames):  # every frame is read once here, so a file that breaks halfway is refused now and not at someone's welcome
                        image.seek(index)
                        image.load()
                    if image.format != "PNG":
                        raise Invalid("Use a JPG, PNG or GIF.")
                    return Prepared(_png_kept(data), "image/png", image.width, image.height, frames, preview)
                picture = ImageOps.exif_transpose(image).convert("RGBA")
    except Invalid:
        raise
    except (*images.READ_ERRORS, EOFError) as error:
        raise Invalid(UNREADABLE) from error
    picture.info.clear()  # comments, text and everything else the file carried, which the save would write again
    if max(picture.size) > MAX_SIDE:
        picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    clear = picture.getextrema()[3][0] < 255
    for _ in range(5):
        out = io.BytesIO()
        try:
            if clear:
                picture.save(out, format="PNG", optimize=False, compress_level=6)
            else:
                picture.convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY)
        except (ValueError, OSError) as error:
            raise Invalid(UNSAVABLE) from error
        if out.tell() <= images.MAX_BYTES:
            return Prepared(out.getvalue(), "image/png" if clear else "image/jpeg", picture.width, picture.height, 1, _preview(picture))
        # A large see-through picture can come out heavier than the file that was sent, so it is scaled down until it fits what a stored picture may weigh.
        picture = picture.resize((max(1, picture.width * 3 // 4), max(1, picture.height * 3 // 4)), Image.Resampling.LANCZOS)
    raise Invalid("That picture is too heavy to keep, even scaled down. Use a smaller or plainer one.")


def _within_limits(frames: int, width: int, height: int, *, more: bool = False) -> None:
    if frames > MAX_SOURCE_FRAMES:  # the rest would never be read, so the file is refused here rather than cut short at every welcome
        raise Invalid(f"That GIF has {'more than ' if more else ''}{frames:,} frames, and a welcome picture reads at most {MAX_SOURCE_FRAMES:,}. Use a shorter one.")
    if frames > 1 and frames * width * height > MAX_ANIMATION_PIXELS:
        raise Invalid(f"That GIF has {frames:,} frames of {width}x{height} pixels, which is more than the bot can animate. Use a shorter or smaller one.")


def _gif(data: bytes) -> tuple[Prepared | None, Image.Image]:
    """An animated GIF as it is kept, or None and the still picture of a GIF of one frame."""
    try:
        parsed = gif.parse(data, MAX_SOURCE_FRAMES + 1)
    except gif.Crowded:
        raise Invalid(CROWDED) from None
    if parsed.crowded:
        raise Invalid(CROWDED)
    frames = len(parsed.frames)
    if frames > MAX_SOURCE_FRAMES:
        frames, all_of_them = gif.counted(data)
        _within_limits(frames, parsed.width, parsed.height, more=not all_of_them)
    if not parsed.whole:  # the file breaks before its end, so frames after the break would be lost
        raise Invalid(UNREADABLE)
    _within_limits(frames, parsed.width, parsed.height)
    shown = gif.composed(parsed)
    _, first = next(shown)
    shown.close()  # the first frame is all the preview and a still picture need, and nothing draws over it once the frames stop
    if frames == 1:
        return None, first
    # Every frame is decoded once here, so a file that breaks halfway is refused now and not at someone's welcome. Laying the frames over
    # each other, which the renderer does, takes twice as long again and can't fail once they decode.
    gif.decoded(parsed)
    return Prepared(gif.kept(parsed), "image/gif", parsed.width, parsed.height, frames, _preview(first)), first


def _png_kept(data: bytes) -> bytes:
    """An animated PNG with only the chunks that draw it, each as the file has it."""
    out = bytearray(data[:8])
    at = 8
    while at + 12 <= len(data):
        kind = data[at + 4:at + 8]
        end = at + 12 + int.from_bytes(data[at:at + 4], "big")  # length, kind, the chunk's data and its check
        if kind in PNG_KEPT:
            out += data[at:end]
        if kind == b"IEND":
            break
        at = end
    return bytes(out)
