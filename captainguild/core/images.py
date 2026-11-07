"""Checks for images people upload or link: what the file really is, how heavy and how big.

A browser's file type and a file's name prove nothing, so the bytes decide. These run Pillow, so call them through asyncio.to_thread.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from aiohttp import BodyPartReader
from PIL import Image, UnidentifiedImageError

from .validate import Invalid

MAX_BYTES = 8 * 1024 * 1024
MIN_SIDE = 64
MAX_SIDE = 4096
MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif"}
OPENED_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")  # no other decoder runs on a file, wherever it came from
MAX_PARTS = 8
READ_ERRORS = (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError)
FIELD_TEXT_LIMIT = 64 * 1024


@dataclass(frozen=True)
class Facts:
    mime: str
    width: int
    height: int
    size: int


def check(data: bytes, *, max_bytes: int = MAX_BYTES, min_side: int = MIN_SIDE, max_side: int = MAX_SIDE) -> Facts:
    """The facts about an image, or Invalid with a sentence for the person who sent it."""
    if not data:
        raise Invalid("That file is empty.")
    if len(data) > max_bytes:
        raise Invalid(f"That file is {len(data) / 1048576:.1f} MB. The limit is {max_bytes // 1048576} MB.")
    try:
        with Image.open(io.BytesIO(data), formats=OPENED_FORMATS) as image:
            kind, (width, height) = image.format, image.size
            if max(width, height) <= max_side:  # verify reads the file, so a huge one is refused from its header alone
                image.verify()
    except READ_ERRORS as error:
        raise Invalid("That file isn't an image the bot can read. Use a JPG, PNG or GIF.") from error
    if kind not in MIMES:
        raise Invalid("Use a JPG, PNG or GIF.")
    if min(width, height) < min_side:
        raise Invalid(f"That image is {width}x{height} pixels. The smallest allowed is {min_side}x{min_side}.")
    if max(width, height) > max_side:
        raise Invalid(f"That image is {width}x{height} pixels. The largest allowed is {max_side}x{max_side}.")
    return Facts(MIMES[kind], width, height, len(data))


async def read_form(request: Any, *, file_limit: int = MAX_BYTES) -> tuple[dict[str, bytes], dict[str, str]]:
    """The files and text fields of a multipart form. Parts are read one at a time, with a limit on each, on how many there are and on the total, so a large upload can't fill memory."""
    if not request.content_type.startswith("multipart/"):
        raise Invalid("Send this as a file upload.")
    files: dict[str, bytes] = {}
    fields: dict[str, str] = {}
    total_limit = 2 * file_limit + FIELD_TEXT_LIMIT  # the most any page sends: two pictures and its text
    total = 0
    count = 0
    try:
        reader = await request.multipart()
    except ValueError as error:
        raise Invalid("That upload couldn't be read.") from error
    while True:
        try:
            part = await reader.next()
        except ValueError as error:
            raise Invalid("That upload couldn't be read.") from error
        if part is None:
            return files, fields
        if not isinstance(part, BodyPartReader):  # a form nested inside a form, which no browser sends
            raise Invalid("That upload couldn't be read.")
        count += 1
        if count > MAX_PARTS:
            raise Invalid("That upload has too many parts.")
        limit = file_limit if part.filename else FIELD_TEXT_LIMIT
        body = bytearray()
        while True:
            chunk = await part.read_chunk(64 * 1024)
            if not chunk:
                break
            body += chunk
            total += len(chunk)
            if total > total_limit:
                raise Invalid(f"That upload is too large. The limit is {total_limit // 1048576} MB in all.")
            if len(body) > limit:
                raise Invalid(f"That file is too large. The limit is {file_limit // 1048576} MB." if part.filename else "That field is too long.")
        if part.filename:
            files[part.name or ""] = bytes(body)
        else:
            fields[part.name or ""] = body.decode("utf-8", errors="replace")
