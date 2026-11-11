"""An animated GIF's frames as a browser shows them.

Pillow reads a GIF's frames one after another, but it lays each one over the picture by rules of its own, and a GIF made for the web can look
different there than in a browser, which is how Discord shows it. So here Pillow decodes each frame by itself, as a GIF of that one frame,
and the frames are laid over each other the way browsers do it:

- The picture starts see-through. It grows to hold the first frame when that frame runs past the GIF's edge, and later frames are cut at
  the edge.
- A frame disposed "to background" is cleared to see-through, whatever color the file names as its background.
- Disposal 3 puts back what was under the frame, and so does 4, which some old writers used for it. Every other value leaves the frame in
  place, and a frame that names no disposal never takes the one before it.
- A byte that starts no block ends the file, and so does an extension block the file cuts short. A loop count counts wherever it
  stands.
- A frame with no colors, its own or the file's, is drawn black.

These run Pillow, so call them through asyncio.to_thread.
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from typing import Callable, Iterator, NamedTuple

from PIL import Image

from .. import images

SIGNATURES = (b"GIF87a", b"GIF89a")  # Pillow opens a file that starts so as a GIF and as nothing else
# Blocks and sub-blocks walked in one file. A GIF an editor writes has a few tens of thousands at most, and 8 MiB can hold millions, which
# took two seconds to walk.
MAX_PIECES = 200_000
KEEP, CLEAR, RESTORE = "keep", "clear", "restore"


class Crowded(ValueError):
    """The file holds more blocks before its first frame than the reader walks."""
DISPOSALS = {2: CLEAR, 3: RESTORE, 4: RESTORE}  # what browsers do with each disposal value. The rest keep the frame.
LOOP_NAMES = (b"NETSCAPE2.0", b"ANIMEXTS1.0")


@dataclass(frozen=True)
class Frame:
    left: int
    top: int
    width: int
    height: int
    disposal: str      # KEEP, CLEAR or RESTORE
    length: int        # milliseconds, as the file gives them, 0 when it gives none
    see_through: bool  # whether the frame has a transparent color
    alone: bytes       # a GIF of this frame by itself, at its own size
    raw: bytes         # the control block the frame was read with and the frame's own block, as the file has them


@dataclass(frozen=True)
class Parsed:
    width: int
    height: int
    loop: int | None  # how often it plays again, 0 for always, None when the file doesn't say
    frames: list[Frame]
    whole: bool       # False when the file cuts a frame short, or holds one larger than the picture or without colors: that frame is lost
    crowded: bool     # the file holds more blocks than the reader walks, so it was read only that far
    head: bytes       # the file's header, screen and shared colors, as the file has them


class _Seen:
    """What a walk through the file finds besides frames."""

    def __init__(self) -> None:
        self.loop: int | None = None
        self.whole = True
        self.pieces = 0
        self.crowded = False


class _Found(NamedTuple):
    left: int
    top: int
    width: int
    height: int
    disposal: str
    length: int
    clear: int | None
    interlaced: bool
    table: bytes
    pixels: bytes  # the LZW code size and the data's sub-blocks, as the file has them
    raw: bytes     # the control block the frame is read with and the frame's own block, as the file has them


def _past_blocks(data: bytes, at: int, seen: _Seen) -> int | None:
    """Where the run of sub-blocks starting at `at` ends, past its closing zero. None when the file ends first, or when the walk has gone as
    far as the reader goes."""
    while at < len(data):
        seen.pieces += 1
        if seen.pieces > MAX_PIECES:
            seen.crowded, seen.whole = True, False
            return None
        size = data[at]
        if not size:
            return at + 1
        at += 1 + size
    return None


def _sub_block(data: bytes, at: int) -> bytes:
    """The bytes of the sub-block whose size stands at `at`, inside a run _past_blocks has already walked."""
    return data[at + 1:at + 1 + data[at]]


def _walk(data: bytes, seen: _Seen) -> Iterator[_Found]:
    """The frames in file order. Stops at the trailer, at a byte that starts no block, or where the file ends, which browsers all take as
    the end of the file. A file that cuts a frame short has lost that frame, and leaves seen.whole False."""
    packed = data[10]
    at = 13
    shared = b""
    if packed & 0x80:
        at += 3 << ((packed & 7) + 1)
        shared = data[13:at]
    disposal, length, clear = KEEP, 0, None  # from the control block before a frame, which counts for that frame alone
    control = b""
    while at < len(data):
        kind = data[at]
        seen.pieces += 1
        if kind == 0x21:  # an extension
            end = _past_blocks(data, at + 2, seen)
            if end is None:  # cut short, with no frame lost
                return
            label, first = data[at + 1], _sub_block(data, at + 2)
            if label == 0xF9 and len(first) >= 4:
                disposal = DISPOSALS.get((first[0] >> 2) & 7, KEEP)
                length = int.from_bytes(first[1:3], "little") * 10
                clear = first[3] if first[0] & 1 else None
                control = data[at:end]
            elif label == 0xFF and first[:11] in LOOP_NAMES:
                second = _sub_block(data, at + 3 + len(first))
                if len(second) >= 3 and second[0] == 1:
                    seen.loop = int.from_bytes(second[1:3], "little")
            at = end
        elif kind == 0x2C:  # a frame
            if at + 10 > len(data):
                seen.whole = False
                return
            left, top, wide, tall, inner = struct.unpack_from("<HHHHB", data, at + 1)
            table, start = shared, at + 10
            if inner & 0x80:
                size = 3 << ((inner & 7) + 1)
                table, start = data[start:start + size], start + size
            end = _past_blocks(data, start + 1, seen)  # the first byte is the LZW code size
            if end is None:  # cut short, so this frame is lost
                seen.whole = False
                return
            yield _Found(left, top, wide, tall, disposal, length, clear, bool(inner & 0x40), table, data[start:end], control + data[at:end])
            disposal, length, clear = KEEP, 0, None
            control = b""
            at = end
        else:
            return


def _alone(found: _Found) -> bytes:
    """A GIF of one frame at its own size, with the frame's colors as the file's own and its transparent color kept."""
    colors = len(found.table) // 3
    out = bytearray(b"GIF89a" + struct.pack("<HHBBB", found.width, found.height, 0xF0 | (colors.bit_length() - 2), 0, 0) + found.table)
    if found.clear is not None:
        out += b"!" + bytes([0xF9, 4, 1, 0, 0, found.clear, 0])
    out += b"," + struct.pack("<HHHHB", 0, 0, found.width, found.height, 0x40 if found.interlaced else 0) + found.pixels + b";"
    return bytes(out)


def parse(data: bytes, most: int) -> Parsed:
    """The picture's size, its loop count and its first `most` frames. Raises ValueError when not even one frame can be read."""
    if len(data) < 13 or data[:6] not in SIGNATURES:
        raise ValueError("That isn't a GIF.")
    width, height = struct.unpack_from("<HH", data, 6)
    seen = _Seen()
    frames: list[Frame] = []
    for found in _walk(data, seen):
        if not frames:  # browsers grow the picture to hold the first frame
            width, height = max(width, found.left + found.width), max(height, found.top + found.height)
        # A frame larger than the picture costs more to decode than the picture is worth. One whose codes start below two bits is one the
        # GIF specification doesn't allow and browsers refuse.
        if max(width, height) > images.MAX_SIDE or found.width > width or found.height > height or found.pixels[0] < 2:
            seen.whole = False
            break
        if not found.table:  # browsers draw every color of a frame that has none black
            found = found._replace(table=bytes(3 << max(1, min(8, found.pixels[0]))))
        frames.append(Frame(found.left, found.top, found.width, found.height, found.disposal, found.length, found.clear is not None, _alone(found),
                            found.raw))
        if len(frames) >= most:
            break
    if not frames and seen.crowded:
        raise Crowded("The GIF holds more blocks than the reader walks before its first frame.")
    if not frames or not width or not height:
        raise ValueError("The GIF has no frame that can be read.")
    head = data[:13 + (3 << ((data[10] & 7) + 1) if data[10] & 0x80 else 0)]
    return Parsed(width, height, seen.loop, frames, seen.whole, seen.crowded, head)


def kept(parsed: Parsed) -> bytes:
    """The GIF as the reader reads it: its header and colors, its loop count, and its frames, each with the control block it was read with.
    Comments, plain text and every other application block, an editor's notes or a camera's location among them, are left out."""
    loop = b"" if parsed.loop is None else b"!" + bytes([0xFF, 11]) + LOOP_NAMES[0] + bytes([3, 1]) + struct.pack("<H", parsed.loop) + bytes(1)
    return parsed.head + loop + b"".join(frame.raw for frame in parsed.frames) + b";"


def count(data: bytes) -> int:
    """How many frames the file holds, read as far as parse reads it."""
    return counted(data)[0]


def counted(data: bytes) -> tuple[int, bool]:
    """How many frames the file holds, read as far as parse reads it, and False when the count stopped where the reader stops walking."""
    if len(data) < 13 or data[:6] not in SIGNATURES:
        return 0, True
    seen = _Seen()
    return sum(1 for _ in _walk(data, seen)), not seen.crowded


def _read(frame: Frame) -> Image.Image:
    try:
        with Image.open(io.BytesIO(frame.alone), formats=["GIF"]) as image:
            image.seek(0)
            return image.convert("RGBA")
    except images.READ_ERRORS as error:
        raise ValueError("A frame of the GIF can't be read.") from error


def _shows(parsed: Parsed, frame: Frame) -> bool:
    """Whether any of the frame lands in the picture. One that doesn't is never read."""
    return frame.left < parsed.width and frame.top < parsed.height and frame.width > 0 and frame.height > 0


def decoded(parsed: Parsed) -> None:
    """Decodes every frame that lands in the picture once, without laying the frames over each other, which is what knowing the renderer
    can read them all takes, at a third of the time. Raises ValueError for a frame Pillow can't decode."""
    for frame in parsed.frames:
        if _shows(parsed, frame):
            try:
                with Image.open(io.BytesIO(frame.alone), formats=["GIF"]) as image:
                    image.load()
            except images.READ_ERRORS as error:
                raise ValueError("A frame of the GIF can't be read.") from error


def composed(parsed: Parsed, check: Callable[[], None] = lambda: None) -> Iterator[tuple[Frame, Image.Image]]:
    """Each frame with the picture as it shows then, RGBA at the picture's size. The picture is the same object every time and changes once
    the next frame is asked for, so copy what is kept. `check` runs before each frame and may raise to stop. Raises ValueError for a frame
    Pillow can't decode."""
    width, height = parsed.width, parsed.height
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for frame in parsed.frames:
        check()
        box = (min(frame.left, width), min(frame.top, height), min(frame.left + frame.width, width), min(frame.top + frame.height, height))
        shows = _shows(parsed, frame)
        under = canvas.crop(box) if shows and frame.disposal == RESTORE else None
        if shows:
            picture = _read(frame)
            # A GIF's pixels are either solid or see-through, so a frame with a transparent color is laid through its own alpha and one
            # without covers what it lies on.
            canvas.paste(picture, (frame.left, frame.top), picture if frame.see_through else None)
        yield frame, canvas
        if shows and frame.disposal == CLEAR:
            canvas.paste((0, 0, 0, 0), box)
        elif under is not None:
            canvas.paste(under, box[:2])


def first(data: bytes) -> Image.Image:
    """The first frame as a browser shows it, RGBA. Raises ValueError for a GIF whose first frame can't be read."""
    frames = composed(parse(data, 1))
    _, canvas = next(frames)
    frames.close()
    return canvas
