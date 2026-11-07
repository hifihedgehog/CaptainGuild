"""A message as data: what the embed editor edits, what tags and greetings store, what the bot posts.

The form is {"content": str, "embeds": [Discord embed JSON], "buttons": [{"label", "url", "emoji"}]}.
Limits are Discord's own from the Embed Limits section of its Message resource docs.
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord

from .validate import Invalid, ValidationError, hex_color, http_url

CONTENT_MAX = 2000
EMBEDS_MAX = 10
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELDS_MAX = 25
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FOOTER_MAX = 2048
AUTHOR_MAX = 256
TOTAL_MAX = 6000
BUTTONS_MAX = 25
BUTTON_LABEL_MAX = 80
TIMESTAMP_FIRST_YEAR = 1970
TIMESTAMP_LAST_YEAR = 3000

_URL = http_url()


def _text(value: Any, maximum: int, name: str) -> str:
    if not isinstance(value, str):
        raise Invalid(f"{name} must be text.")
    if len(value) > maximum:
        raise Invalid(f"{name} can be at most {maximum} characters. This is {len(value)}.")
    return value


def _link(value: Any, name: str, *, attachment_ok: bool = False, templates: bool = False) -> str:
    if attachment_ok and isinstance(value, str) and value.startswith("attachment://"):
        return value
    # With `templates`, a link can be a template, such as {user(avatar)}. render_payload drops one that doesn't come out as a link.
    if templates and isinstance(value, str) and value.startswith(("{", "http://", "https://")) and "{" in value and "}" in value and len(value) <= 2048:
        return value
    try:
        return _URL(value)
    except Invalid as error:
        raise Invalid(f"{name}: {error}") from error


def clean_embed(data: Any, *, templates: bool = False) -> dict:
    """Keep only what Discord accepts for a rich embed. Empty strings count as not set. With `templates`, a link can be a template."""
    if not isinstance(data, dict):
        raise Invalid("An embed must be a set of fields.")
    out: dict[str, Any] = {}
    if data.get("title"):
        out["title"] = _text(data["title"], TITLE_MAX, "Title")
    if data.get("description"):
        out["description"] = _text(data["description"], DESCRIPTION_MAX, "Description")
    if data.get("url"):
        if "title" not in out:
            raise Invalid("A title link needs a title.")
        out["url"] = _link(data["url"], "Title link", templates=templates)
    color = data.get("color")
    if color not in (None, ""):
        try:
            out["color"] = hex_color(color)
        except Invalid as error:
            raise Invalid(f"Color: {error}") from error
    timestamp = data.get("timestamp")
    if timestamp:
        try:
            parsed = datetime.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(datetime.timezone.utc)  # at the edges of the calendar this overflows, and it would otherwise do so on every send
        except (ValueError, OverflowError) as error:
            raise Invalid("The timestamp isn't a date and time.") from error
        if not TIMESTAMP_FIRST_YEAR <= parsed.year <= TIMESTAMP_LAST_YEAR:
            raise Invalid(f"The timestamp has to fall between {TIMESTAMP_FIRST_YEAR} and {TIMESTAMP_LAST_YEAR}.")
        out["timestamp"] = parsed.isoformat()
    footer = data.get("footer") or {}
    if isinstance(footer, dict) and (footer.get("text") or footer.get("icon_url")):
        if not footer.get("text"):
            raise Invalid("A footer icon needs footer text.")
        out["footer"] = {"text": _text(footer["text"], FOOTER_MAX, "Footer")}
        if footer.get("icon_url"):
            out["footer"]["icon_url"] = _link(footer["icon_url"], "Footer icon", attachment_ok=True, templates=templates)
    author = data.get("author") or {}
    if isinstance(author, dict) and (author.get("name") or author.get("url") or author.get("icon_url")):
        if not author.get("name"):
            raise Invalid("An author link or icon needs an author name.")
        out["author"] = {"name": _text(author["name"], AUTHOR_MAX, "Author name")}
        if author.get("url"):
            out["author"]["url"] = _link(author["url"], "Author link", templates=templates)
        if author.get("icon_url"):
            out["author"]["icon_url"] = _link(author["icon_url"], "Author icon", attachment_ok=True, templates=templates)
    for key, name in (("image", "Image"), ("thumbnail", "Thumbnail")):
        part = data.get(key) or {}
        if isinstance(part, dict) and part.get("url"):
            out[key] = {"url": _link(part["url"], name, attachment_ok=True, templates=templates)}
    fields = data.get("fields") or []
    if not isinstance(fields, list):
        raise Invalid("Fields must be a list.")
    if len(fields) > FIELDS_MAX:
        raise Invalid(f"An embed can hold at most {FIELDS_MAX} fields.")
    clean_fields = []
    for index, field in enumerate(fields, 1):
        if not isinstance(field, dict) or not field.get("name") or not field.get("value"):
            raise Invalid(f"Field {index} needs both a name and a value.")
        clean_fields.append({
            "name": _text(field["name"], FIELD_NAME_MAX, f"Field {index} name"),
            "value": _text(field["value"], FIELD_VALUE_MAX, f"Field {index} value"),
            "inline": bool(field.get("inline")),
        })
    if clean_fields:
        out["fields"] = clean_fields
    return out


def embed_size(embed: dict) -> int:
    """How Discord counts toward its 6000-character limit."""
    size = len(embed.get("title") or "") + len(embed.get("description") or "")
    size += len((embed.get("footer") or {}).get("text") or "") + len((embed.get("author") or {}).get("name") or "")
    for field in embed.get("fields") or []:
        size += len(field["name"]) + len(field["value"])
    return size


def is_empty_embed(embed: dict) -> bool:
    return not any(k in embed for k in ("title", "description", "fields", "image", "thumbnail", "author", "footer"))


def clean_payload(data: Any, *, templates: bool = False) -> dict:
    """Check a whole message and return its clean form. Raises Invalid with a sentence for the person editing it.

    `templates` is for a message that goes through the template language before it's posted, so its links can hold variables.
    """
    if not isinstance(data, dict):
        raise Invalid("A message must be a set of fields.")
    content = data.get("content") or ""
    if not isinstance(content, str):
        raise Invalid("The message text must be text.")
    if len(content) > CONTENT_MAX:
        raise Invalid(f"The message text can be at most {CONTENT_MAX} characters. This is {len(content)}.")
    embeds_in = data.get("embeds") or []
    if not isinstance(embeds_in, list):
        raise Invalid("Embeds must be a list.")
    if len(embeds_in) > EMBEDS_MAX:
        raise Invalid(f"A message can hold at most {EMBEDS_MAX} embeds.")
    embeds = []
    for index, raw in enumerate(embeds_in, 1):
        try:
            embed = clean_embed(raw, templates=templates)
        except Invalid as error:
            raise Invalid(f"Embed {index}: {error}") from error
        if not is_empty_embed(embed):
            embeds.append(embed)
    total = sum(embed_size(e) for e in embeds)
    if total > TOTAL_MAX:
        raise Invalid(f"All embeds together can hold at most {TOTAL_MAX} characters. These hold {total}.")
    buttons_in = data.get("buttons") or []
    if not isinstance(buttons_in, list) or len(buttons_in) > BUTTONS_MAX:
        raise Invalid(f"A message can hold at most {BUTTONS_MAX} link buttons.")
    buttons = []
    for index, raw in enumerate(buttons_in, 1):
        if not isinstance(raw, dict) or not raw.get("url") or not (raw.get("label") or raw.get("emoji")):
            raise Invalid(f"Button {index} needs a link and a label or emoji.")
        button = {"url": _link(raw["url"], f"Button {index}", templates=templates)}
        if raw.get("label"):
            button["label"] = _text(raw["label"], BUTTON_LABEL_MAX, f"Button {index} label")
        if raw.get("emoji"):
            button["emoji"] = _text(raw["emoji"], 100, f"Button {index} emoji")
        buttons.append(button)
    return {"content": content, "embeds": embeds, "buttons": buttons}


def empty_payload() -> dict:
    return {"content": "", "embeds": [], "buttons": []}


_PING_WORDS = re.compile(r"@everyone|@here|<@&\d+>")
_EVERYONE_WORDS = re.compile(r"@everyone|@here")


def ping_words(payload: dict, *, everyone_only: bool = False) -> set[str]:
    """What a message's text would ping if a post let it: @everyone, @here and role mentions. Discord reads pings from the text alone, never from an embed."""
    return set((_EVERYONE_WORDS if everyone_only else _PING_WORDS).findall(payload.get("content") or ""))


def has_content(payload: dict) -> bool:
    return bool(payload.get("content") or payload.get("embeds"))


def required_payload(raw: Any, field: str = "payload", *, templates: bool = False) -> dict:
    """A message from a form, checked. It must hold something Discord would post."""
    try:
        payload = clean_payload(raw, templates=templates)
    except Invalid as error:
        raise ValidationError({field: str(error)}) from error
    if not has_content(payload):
        raise ValidationError({field: "Write some text or fill in an embed first."})
    return payload


def from_legacy(content: str | None, embed_json: str | dict | None) -> dict:
    """Build a message from text plus a single embed stored as JSON text, the shape other bots export."""
    embed: Any = embed_json
    if isinstance(embed_json, str):
        try:
            embed = json.loads(embed_json or "{}")
        except ValueError:
            embed = {}
    embeds = [embed] if isinstance(embed, dict) and embed else []
    return clean_payload({"content": content or "", "embeds": embeds})


def map_text(payload: dict, change: Callable[[str], str]) -> dict:
    """Apply `change` to every piece of text a reader sees. Greetings and tags use this to fill in variables."""
    def fix(value: str) -> str:
        return change(value) if value else value

    out = {"content": fix(payload.get("content", "")), "embeds": [], "buttons": payload.get("buttons", [])}
    for embed in payload.get("embeds", []):
        copy = dict(embed)
        for key in ("title", "description"):
            if key in copy:
                copy[key] = fix(copy[key])
        if "footer" in copy:
            copy["footer"] = {**copy["footer"], "text": fix(copy["footer"].get("text", ""))}
        if "author" in copy:
            copy["author"] = {**copy["author"], "name": fix(copy["author"].get("name", ""))}
        if "fields" in copy:
            copy["fields"] = [{**f, "name": fix(f["name"]), "value": fix(f["value"])} for f in copy["fields"]]
        for key in ("image", "thumbnail"):
            if key in copy and copy[key].get("url"):
                copy[key] = {"url": fix(copy[key]["url"])}
        out["embeds"].append(copy)
    return out


def link_view(payload: dict) -> discord.ui.View | None:
    buttons = payload.get("buttons") or []
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for button in buttons:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=button["url"], label=button.get("label"), emoji=button.get("emoji")))
    return view


def send_kwargs(payload: dict, *, allowed_mentions: discord.AllowedMentions | None = None) -> dict:
    """Keyword arguments for Messageable.send. Works for Message.edit too, once `embeds` and `content` are always present."""
    out: dict[str, Any] = {"content": payload.get("content") or None}
    out["embeds"] = [discord.Embed.from_dict(e) for e in payload.get("embeds", [])]
    view = link_view(payload)
    if view is not None:
        out["view"] = view
    if allowed_mentions is not None:
        out["allowed_mentions"] = allowed_mentions
    return out


def component_view(payload: dict, components: list[dict]) -> discord.ui.View:
    """A feature's own buttons and menus, then the message's link buttons.

    A component is {"type": "button", "custom_id", "label", "emoji", "style", "disabled"}, with style naming a discord.ButtonStyle,
    {"type": "link", "url", "label", "emoji"} for a link that sits among them, or
    {"type": "select", "custom_id", "placeholder", "min", "max", "options": [{"label", "value", "description", "emoji"}]}.
    Whatever answers them is registered apart from this view, because a view object doesn't outlive a restart.
    """
    view = discord.ui.View(timeout=None)
    for part in components:
        if part["type"] == "button":
            view.add_item(discord.ui.Button(style=getattr(discord.ButtonStyle, part.get("style") or "secondary"), custom_id=part["custom_id"], label=part.get("label") or None, emoji=part.get("emoji") or None, disabled=bool(part.get("disabled"))))
            continue
        if part["type"] == "link":
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=part["url"], label=part.get("label") or None, emoji=part.get("emoji") or None))
            continue
        options = [discord.SelectOption(label=o["label"], value=o["value"], description=o.get("description") or None, emoji=o.get("emoji") or None) for o in part["options"]]
        view.add_item(discord.ui.Select(custom_id=part["custom_id"], placeholder=part.get("placeholder") or None, min_values=part.get("min", 1), max_values=part.get("max", 1), options=options))
    for button in payload.get("buttons") or []:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=button["url"], label=button.get("label"), emoji=button.get("emoji")))
    # discord.py remembers a live view per message and looks there first, so these empty buttons would swallow every click
    # until the next restart. A stopped view is only drawn, and the clicks reach whatever was registered.
    view.stop()
    return view


def unsigned(url: str | None) -> str | None:
    """A Discord file link without its expiring signature. Discord renews a link sent to it bare, so a stored copy keeps working.

    See Signed Attachment CDN URLs in Discord's API reference.
    """
    if not url:
        return url
    parts = urlsplit(url)
    if parts.netloc not in ("cdn.discordapp.com", "media.discordapp.net") or not parts.path.startswith("/attachments/"):
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("ex", "is", "hm")]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def from_message(message: Any) -> dict:
    """Read a posted Discord message back into this form, for editing or for reposting."""
    embeds = []
    for embed in message.embeds:
        data = embed.to_dict()
        if data.get("type") not in (None, "rich"):
            continue
        for key in ("type", "video", "provider"):
            data.pop(key, None)
        for key in ("image", "thumbnail"):
            if key in data:
                data[key] = {"url": unsigned(data[key].get("url"))}
        if "footer" in data:
            data["footer"] = {k: unsigned(v) if k == "icon_url" else v for k, v in data["footer"].items() if k in ("text", "icon_url")}
        if "author" in data:
            data["author"] = {k: unsigned(v) if k == "icon_url" else v for k, v in data["author"].items() if k in ("name", "url", "icon_url")}
        embeds.append(data)
    buttons = []
    for row in getattr(message, "components", []) or []:
        for child in getattr(row, "children", []) or []:
            if getattr(child, "url", None):
                button = {"url": child.url}
                if child.label:
                    button["label"] = child.label
                if child.emoji:
                    button["emoji"] = str(child.emoji)
                buttons.append(button)
    return {"content": message.content or "", "embeds": embeds, "buttons": buttons}


def _component_emoji(emoji: Any) -> str | None:
    if isinstance(emoji, str) or emoji is None:
        return emoji
    if isinstance(emoji, dict):
        if emoji.get("id"):
            return f"<{'a' if emoji.get('animated') else ''}:{emoji.get('name') or 'emoji'}:{emoji['id']}>"
        return emoji.get("name")
    return None


def from_json(data: Any) -> dict:
    """The clean form of the JSON people paste: a whole message, one embed, a list of embeds, or a backup from a web embed tool."""
    if isinstance(data, list):
        data = {"embeds": data}
    if not isinstance(data, dict):
        raise Invalid("That JSON isn't a message or an embed.")
    backups = data.get("messages")
    if isinstance(backups, list) and backups and isinstance(backups[0], dict):
        data = backups[0].get("data") if isinstance(backups[0].get("data"), dict) else backups[0]
    if isinstance(data.get("embed"), dict):
        data = {**data, "embeds": [data["embed"]]}
    if not any(key in data for key in ("content", "embeds", "buttons", "components")):
        return clean_payload({"embeds": [data]})
    buttons = list(data.get("buttons") or []) if isinstance(data.get("buttons"), list) else []
    rows = data.get("components") if isinstance(data.get("components"), list) else []
    for row in rows:
        parts = row.get("components") if isinstance(row, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if isinstance(part, dict) and part.get("url"):
                buttons.append({"url": part["url"], "label": part.get("label"), "emoji": _component_emoji(part.get("emoji"))})
    return clean_payload({"content": data.get("content"), "embeds": data.get("embeds"), "buttons": buttons})


def _texts(payload: dict) -> Iterator[str]:
    yield payload.get("content") or ""
    for embed in payload.get("embeds", []):
        yield embed.get("title") or ""
        yield embed.get("description") or ""
        yield (embed.get("author") or {}).get("name") or ""
        for field in embed.get("fields") or []:
            yield field.get("name") or ""
            yield field.get("value") or ""
        yield (embed.get("footer") or {}).get("text") or ""


def summary(payload: dict, limit: int = 120) -> str:
    """One line describing a message, for tables and the activity log."""
    line = next((re.sub(r"\s+", " ", discord.utils.remove_markdown(text)).strip() for text in _texts(payload) if text.strip()), "")
    if not line:
        return "Image only" if any(e.get("image") or e.get("thumbnail") for e in payload.get("embeds", [])) else "Empty message"
    return line if len(line) <= limit else line[:limit - 3].rstrip() + "..."


def searchable(payload: dict) -> str:
    """All of a message's text in lower case, for a stored search column."""
    return " ".join(text for text in _texts(payload) if text).lower()
