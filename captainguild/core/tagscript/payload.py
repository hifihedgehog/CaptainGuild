"""Whole messages through the template language: what greetings, tags and triggers store and what goes out.

A stored message can hold blocks anywhere a person writes text, and what comes out has to fit what Discord accepts, whatever the blocks produced. A member's
long nickname or a link that came out as nothing must cost a line of text at most, never the whole post, so `fit_payload` trims and drops instead of refusing.
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Callable

from .. import messages
from ..validate import Invalid, hex_color, http_url, snowflake
from .clock import from_unix
from .engine import Actions, Interpreter, Scope
from .text import LimitReached

log = logging.getLogger(__name__)

TEMPLATE_MAX = 25_000  # characters of template in a tag or trigger, before blocks shrink it down to what Discord takes

_URL = http_url()


def _link_or_none(value: Any, *, attachment_ok: bool = False) -> str | None:
    if attachment_ok and isinstance(value, str) and value.startswith("attachment://"):
        return value
    try:
        return _URL(value)
    except Invalid:
        return None


def _cut(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _fit_embed(data: Any, room: int) -> tuple[dict, int]:
    """One embed cut down to Discord's limits, with `room` characters left of the 6000 a message's embeds share."""
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out, room

    def take(value: Any, limit: int) -> str:
        nonlocal room
        text = _cut(value, min(limit, room))
        room -= len(text)
        return text

    title = take(data.get("title"), messages.TITLE_MAX)
    if title:
        out["title"] = title
        url = _link_or_none(data.get("url"))
        if url:
            out["url"] = url
    description = take(data.get("description"), messages.DESCRIPTION_MAX)
    if description:
        out["description"] = description
    color = data.get("color")
    if color not in (None, ""):
        try:
            out["color"] = hex_color(color)
        except Invalid:
            pass
    timestamp = data.get("timestamp")
    if timestamp:
        try:
            out["timestamp"] = datetime.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    footer = data.get("footer")
    if isinstance(footer, dict):
        text = take(footer.get("text"), messages.FOOTER_MAX)
        if text:
            out["footer"] = {"text": text}
            icon = _link_or_none(footer.get("icon_url"), attachment_ok=True)
            if icon:
                out["footer"]["icon_url"] = icon
    author = data.get("author")
    if isinstance(author, dict):
        name = take(author.get("name"), messages.AUTHOR_MAX)
        if name:
            out["author"] = {"name": name}
            url = _link_or_none(author.get("url"))
            if url:
                out["author"]["url"] = url
            icon = _link_or_none(author.get("icon_url"), attachment_ok=True)
            if icon:
                out["author"]["icon_url"] = icon
    for key in ("image", "thumbnail"):
        part = data.get(key)
        if isinstance(part, dict):
            link = _link_or_none(part.get("url"), attachment_ok=True)
            if link:
                out[key] = {"url": link}
    fields = []
    source = data.get("fields")
    for field in source[:messages.FIELDS_MAX] if isinstance(source, list) else []:
        if not isinstance(field, dict):
            continue
        name = _cut(field.get("name"), messages.FIELD_NAME_MAX)
        value = _cut(field.get("value"), messages.FIELD_VALUE_MAX)
        if not name.strip() or not value.strip() or len(name) + len(value) > room:  # Discord refuses a whole message over one empty field
            continue
        room -= len(name) + len(value)
        fields.append({"name": name, "value": value, "inline": bool(field.get("inline"))})
    if fields:
        out["fields"] = fields
    return out, room


def fit_payload(payload: Any) -> dict:
    """A message Discord will take, cut down from whatever is here. Never raises. It can come out with nothing to send."""
    source = payload if isinstance(payload, dict) else {}
    embeds = []
    room = messages.TOTAL_MAX
    stored = source.get("embeds")
    for raw in stored[:messages.EMBEDS_MAX] if isinstance(stored, list) else []:
        embed, room = _fit_embed(raw, room)
        if not messages.is_empty_embed(embed):
            embeds.append(embed)
    buttons = []
    stored = source.get("buttons")
    for raw in stored[:messages.BUTTONS_MAX] if isinstance(stored, list) else []:
        if not isinstance(raw, dict):
            continue
        link = _link_or_none(raw.get("url"))
        label = _cut(raw.get("label"), messages.BUTTON_LABEL_MAX)
        if not link or not (label or raw.get("emoji")):
            continue
        button = {"url": link}
        if label:
            button["label"] = label
        if raw.get("emoji"):
            button["emoji"] = _cut(raw["emoji"], 100)
        buttons.append(button)
    return {"content": _cut(source.get("content"), messages.CONTENT_MAX), "embeds": embeds, "buttons": buttons}


def _apply_embed_blocks(payload: dict, changes: list[tuple[str, str]], now: float) -> None:
    """Embed blocks change the message's first embed, and start one when it has none, since a tag made by command has no builder to start it. fit_payload drops one with nothing to show."""
    if not changes:
        return
    if not payload.get("embeds"):
        payload["embeds"] = [{}]
    embed = payload["embeds"][0]
    for key, value in changes:
        if key == "timestamp":
            if value.strip().lower() == "now":
                embed["timestamp"] = from_unix(now).isoformat()
        elif key == "color":
            try:
                embed["color"] = hex_color(value)
            except Invalid:
                pass
        elif value:
            embed[key] = value
        else:
            embed.pop(key, None)


def _render_all(payload: Any, render: Callable[[str], str]) -> dict:
    """Text first, so the embed can read its variables, then each embed's parts in the order the page shows them, then the buttons. Whatever shape a stored message has, this reads what it can."""
    source = payload if isinstance(payload, dict) else {}

    def fill(value: Any) -> Any:
        return render(value) if isinstance(value, str) and value else value

    out: dict[str, Any] = {"content": fill(source.get("content") or ""), "embeds": [], "buttons": []}
    embeds = source.get("embeds")
    for embed in embeds if isinstance(embeds, list) else []:
        if not isinstance(embed, dict):
            continue
        copy = dict(embed)

        def fill_part(part: str, *keys: str) -> None:
            if isinstance(copy.get(part), dict):
                copy[part] = {**copy[part], **{key: fill(copy[part][key]) for key in keys if key in copy[part]}}

        fill_part("author", "name", "url", "icon_url")
        for key in ("title", "url", "description"):
            if key in copy:
                copy[key] = fill(copy[key])
        fields = copy.get("fields")
        if isinstance(fields, list):
            copy["fields"] = [{**field, "name": fill(field.get("name")), "value": fill(field.get("value"))} for field in fields if isinstance(field, dict)]
        fill_part("image", "url")
        fill_part("thumbnail", "url")
        fill_part("footer", "text", "icon_url")
        out["embeds"].append(copy)
    buttons = source.get("buttons")
    for button in buttons if isinstance(buttons, list) else []:
        if isinstance(button, dict):
            out["buttons"].append({**button, **{key: fill(button[key]) for key in ("label", "url") if key in button}})
    return out


def render_text(text: str, scope: Scope) -> str:
    """Fill in a piece of text. Blocks that need Discord do nothing here, and a person's template can't make this raise. Text it can't finish stays as written."""
    if not isinstance(text, str) or "{" not in text:
        return text
    try:
        return Interpreter(scope).render_or_keep(text)
    except Exception:
        log.exception("A template failed")
        return text


def render_payload(payload: dict, scope: Scope) -> dict:
    """Fill in a whole message and cut it to fit Discord. Never raises."""
    try:
        run = Interpreter(scope)
        out = _render_all(payload, run.render_or_keep)
        _apply_embed_blocks(out, run.actions.embed, run.now)
        return fit_payload(out)
    except Exception:
        log.exception("A message template failed")
        return fit_payload(payload)


def render_strict(payload: dict, scope: Scope) -> tuple[dict, Actions, Interpreter]:
    """For run_tag and previews: LimitReached comes out, so nothing half done gets posted or acted on."""
    run = Interpreter(scope)

    def strict(text: str) -> str:
        return run.render(text) if "{" in text else text

    out = _render_all(payload, strict)
    _apply_embed_blocks(out, run.actions.embed, run.now)
    return fit_payload(out), run.actions, run


def ids_from(value: Any) -> list[int]:
    """Discord IDs from an imported file. One that makes no sense is dropped, because the file may come from anywhere."""
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        try:
            number = snowflake(item)
        except Invalid:
            continue
        if number < 2 ** 63 and number not in out:  # the most a database column holds, and more than any real ID
            out.append(number)
    return out


def read_json_text(value: Any) -> Any:
    """Pasted JSON as data, or a value that's already data. Raises ValueError when it can't be read."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except RecursionError as error:  # a file can nest deeper than the parser will follow
        raise ValueError("nested too deeply") from error


def clean_template_message(data: Any) -> dict:
    """Check a stored tag or trigger message. Raises Invalid with a sentence for the person editing it.

    The text can run past Discord's 2000 characters, because blocks shrink it. Its embeds keep Discord's limits as written.
    """
    if not isinstance(data, dict):
        raise Invalid("A message must be a set of fields.")
    content = data.get("content") or ""
    if not isinstance(content, str):
        raise Invalid("The message text must be text.")
    if len(content) > TEMPLATE_MAX:
        raise Invalid(f"The text can be at most {TEMPLATE_MAX} characters. This is {len(content)}.")
    clean = messages.clean_payload({**data, "content": ""}, templates=True)
    clean["content"] = content
    return clean


__all__ = ["TEMPLATE_MAX", "LimitReached", "clean_template_message", "fit_payload", "ids_from", "read_json_text", "render_payload", "render_strict", "render_text"]
