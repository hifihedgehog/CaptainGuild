"""The statistics cards: one picture per view, 1,280 pixels wide, drawn from what views.py gathered.

Each card has the same frame: what it's about across the top, panels of numbers, and a footer naming the period, the server's time zone
and the bot. A chart draws one measure on one axis: where two belong together, such as messages and the people who sent them, they're two
charts, one above the other. The bucket still running at the right edge of a chart is drawn lighter, since it isn't over. Pictures (the
server icon, an avatar, emoji) arrive already fetched, as bytes.

render() is the one entry point, and it's meant for a worker thread.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Sequence

from . import draw as d
from . import words

W = d.WIDTH
M = 32  # outer margin
G = 20  # gap between panels
TALL = 1400  # cards start this tall and are cut below their last panel
COLUMN_LIMIT = 90  # past this many buckets a chart draws an area, since bars would be thinner than a line
MINI_COLUMNS = 40  # the same for a card's small charts
DONUT_PARTS = 5  # a ring's slices before the rest fold into one
EMPTY = chr(0x2013)  # an empty slot: an en dash


# ---- words on cards ----

def _short_period(data: dict) -> str:
    footer = data.get("footer") or {}
    return footer.get("short") or footer.get("period") or ""


def _row_label(lang: str, days: int | None, data: dict) -> str:
    return words.days_label(lang, days) if days else _short_period(data)


def _count(lang: str, value: float, key: str) -> tuple[str, str]:
    shown = words.figure(lang, value)
    return shown, words.unit(lang, key, value, shown)


def _voice(lang: str, seconds: float, time_unit: str) -> tuple[str, str, str]:
    """The number, its unit, and the unit's short form."""
    shown = words.voice_figure(lang, seconds, time_unit)
    return shown, words.unit(lang, time_unit, words.voice_value(seconds, time_unit), shown), words.short_unit(lang, time_unit)


def _voice_short(lang: str, seconds: float, time_unit: str) -> str:
    return f"{words.voice_figure(lang, seconds, time_unit)} {words.short_unit(lang, time_unit)}"


def _channel_label(lang: str, entry: dict | None, *, mark: bool = True) -> tuple[str, bool]:
    """A channel's name as a card prints it, and whether it's a real name (False for hidden and deleted ones). Without `mark`, a text
    channel's name has no "#" before it, for a row that already shows the channel's type."""
    if not entry:
        return EMPTY, False
    state = entry.get("state")
    name = entry.get("name")
    if state == "hidden":
        return words.t(lang, "hidden_channel"), False
    if state == "deleted" and not name:
        return words.t(lang, "deleted_channel"), False
    shown = d.drawable(name or "", "") or "#" + str(entry.get("id", ""))[-4:]
    if state == "deleted":
        return words.t(lang, "deleted_suffix", name=shown), True
    return ("#" + shown if mark and entry.get("kind") in ("text", "forum") else shown), True


def _first_readable(*names: Any) -> str | None:
    """The first of these names the card's font can draw in full."""
    for name in names:
        shown = d.readable(name) if isinstance(name, str) else None
        if shown:
            return shown
    return None


def _user_number(lang: str, user_id: Any) -> str:
    return words.t(lang, "user_number", number=str(user_id or "")[-4:] or "0")


def _person_label(lang: str, entry: dict | None) -> tuple[str, bool]:
    """A person's name as a card prints it, and whether it's a member's own name. The name the server shows comes first, then the
    username, then the global name, whichever the font can draw, then User and the last four digits of their ID."""
    if not entry or entry.get("state") == "private":  # nobody who chose privacy is ranked, and if one ever came through, nothing of them shows
        return EMPTY, False
    name = _first_readable(entry.get("name"), entry.get("username"), entry.get("global_name"))
    if not name:
        return _user_number(lang, entry.get("id")), False
    return name, entry.get("state") == "member"


def person_name(lang: str, data: dict) -> str:
    """The name at the top of a member's card or chart, by the same order as _person_label."""
    return _first_readable(data.get("title"), data.get("username"), data.get("global_name")) or _user_number(lang, data.get("id"))


def _emoji_label(lang: str, entry: dict) -> str:
    if entry.get("kind") == "unicode":
        return entry.get("name") or ""
    name = d.drawable(entry.get("name") or "", str(entry.get("id") or ""))
    if entry.get("kind") == "sticker":
        return f"{name} ({words.t(lang, 'sticker').lower()})"
    return f":{name}:"


def _invite_label(lang: str, entry: dict) -> tuple[str, bool]:
    state = entry.get("state")
    label = d.drawable(entry.get("label") or "", "")
    if state == "vanity":
        return (f"{words.t(lang, 'vanity_link')} ({label})" if label else words.t(lang, "vanity_link")), True
    if state == "inviter":
        person = entry.get("person")
        name = _person_label(lang, person)[0] if person and person.get("state") != "private" else None
        return (words.t(lang, "invite_from", name=name), True) if name else (words.t(lang, "unknown_invite"), False)
    return (label, True) if label else (words.t(lang, "unknown_invite"), False)


def _per(lang: str, interval: str) -> str:
    return words.t(lang, f"per_interval_{interval}")


def _labels(lang: str, chart: dict, tz: Any) -> list[str]:
    """The labels under a chart. Days and weeks over more than about eleven months are labeled by month and year, so a tick says which year."""
    times = chart["times"]
    interval = chart["interval"]
    if interval in ("day", "week") and len(times) > 1 and times[-1] - times[0] > 330 * 86400:
        interval = "month"
    return [words.bucket(lang, start, interval, tz) for start in times]


def _count_axis(lang: str) -> Callable[[float], str]:
    return lambda value: words.compact(lang, value)


def _in_unit(values: Sequence[float | None], time_unit: str) -> list[float | None]:
    """Voice seconds in the server's unit, so a chart's steps are round in hours, minutes or days."""
    return [None if value is None else words.voice_value(value, time_unit) for value in values]


# ---- markers drawn before a name ----

def _icon_marker(card: d.Card, row: dict, left: float, middle: float, height: float) -> float:
    card.icon(row.get("icon", "hash"), left + 14, middle, 26, d.DIM)
    return 38


def _emoji_marker(pictures: dict) -> Callable[[d.Card, dict, float, float, float], float]:
    def marker(card: d.Card, row: dict, left: float, middle: float, height: float) -> float:
        size = min(40, height * 0.7)
        if row.get("kind") == "unicode":
            drawn = card.emoji_glyph(left + size / 2, middle, row.get("text") or "", size)
        else:
            drawn = card.sticker_or_emoji((pictures.get("emoji") or {}).get(row.get("key")), left + size / 2, middle, size)
        return size + 14 if drawn else 0
    return marker


# ---- layout ----

def _columns_of(count: int, top: float, height: float) -> list[tuple[float, float, float, float]]:
    width = (W - 2 * M - G * (count - 1)) / count
    return [(M + index * (width + G), top, M + index * (width + G) + width, top + height) for index in range(count)]


def _split(box: Sequence[float], shares: Sequence[float]) -> list[tuple[float, float, float, float]]:
    x0, y0, x1, y1 = box
    room = x1 - x0 - G * (len(shares) - 1)
    out = []
    left = x0
    total = sum(shares)
    for share in shares:
        width = room * share / total
        out.append((left, y0, left + width, y1))
        left += width + G
    return out


def _grid(box: Sequence[float], columns: int, rows: int, gap: float = 10) -> list[tuple[float, float, float, float]]:
    x0, y0, x1, y1 = box
    width = (x1 - x0 - gap * (columns - 1)) / columns
    height = (y1 - y0 - gap * (rows - 1)) / rows
    return [(x0 + column * (width + gap), y0 + row * (height + gap), x0 + column * (width + gap) + width, y0 + row * (height + gap) + height)
            for row in range(rows) for column in range(columns)]


def _finish(card: d.Card, bottom: float, data: dict) -> bytes:
    card.end_at(bottom)
    footer = data.get("footer") or {}
    d.footer(card, " · ".join(part for part in (footer.get("period"), footer.get("zone")) if part), "Captain Guild")
    return card.png()


def _nothing(card: d.Card, plot: d.Plot, text: str) -> None:
    card.text((plot.left + plot.right) / 2, (plot.top + plot.bottom) / 2, text, 21, color=d.DIM, anchor="mm")


# ---- charts inside cards ----

def _measure(card: d.Card, box: Sequence[float], values: Sequence[float | None], color: str, labels: Sequence[str], fmt: Callable[[float], str], *,
             title: str = "", note: str = "", empty: str = "", limit: int = COLUMN_LIMIT, title_size: float = 23) -> None:
    """One measure over time in its own frame: bars for a few buckets, an area for many. `note` sits at the right of the title."""
    x0, y0, x1, y1 = box
    room = 0
    if title or note:
        if title:
            card.fitted(x0, y0 + 24, title, (x1 - x0) * (0.62 if note else 1), title_size, bold=True, color=d.SOFT, minimum=15)
        if note:
            card.text(x1, y0 + 24, note, 20, color=d.DIM, anchor="rs")
        room = 36
    peak = max((value for value in values if value is not None), default=0) or 0
    bars = len(values) <= limit
    plot = d.Plot(card, (x0, y0 + room, x1, y1), peak, labels, fmt, columns=bars)
    if not peak:
        if empty:
            _nothing(card, plot, empty)
        return
    if bars:
        d.columns(plot, [value or 0 for value in values], color, partial=True)
    else:
        d.area(plot, values, color, partial=True)


def _mini_pair(card: d.Card, box: Sequence[float], lang: str, data: dict, chart: dict, *, voice: bool = True) -> None:
    """Messages and voice side by side in one panel, each on its own axis."""
    inner = d.panel(card, box)
    labels = _labels(lang, chart, data["tz"])
    per = _per(lang, chart["interval"])
    empty = words.t(lang, "nothing_yet")
    both = voice and "voice" in chart
    halves = _split(inner, (1, 1)) if both else [inner]
    messages = chart.get("messages") or []
    peak = max(messages, default=0)
    _measure(card, halves[0], messages, d.SERIES[0], labels, _count_axis(lang), title=words.t(lang, "chart_of", what=words.t(lang, "messages"), per=per),
             note=words.t(lang, "peak", value=words.figure(lang, peak)) if peak else "", empty=empty, limit=MINI_COLUMNS, title_size=22)
    if both:
        unit_name = data.get("unit", "hours")
        seconds = chart.get("voice") or []
        peak = max(seconds, default=0)
        _measure(card, halves[1], _in_unit(seconds, unit_name), d.SERIES[1], labels, _count_axis(lang),
                 title=words.t(lang, "chart_of", what=words.t(lang, "voice"), per=per), note=words.t(lang, "peak", value=_voice_short(lang, peak, unit_name)) if peak else "",
                 empty=empty, limit=MINI_COLUMNS, title_size=22)


# ---- the server card ----

def server(data: dict, lang: str, pictures: dict) -> bytes:
    card = d.Card(TALL)
    chips = [(words.t(lang, "created"), words.date(lang, data["created"], data["tz"]))]
    if data.get("began"):
        chips.append((words.t(lang, "tracked_since"), words.date(lang, data["began"], data["tz"])))
    title = d.drawable(data["title"], "Server")
    top = d.header(card, title=title, subtitle=words.t(lang, "server_overview"), picture=pictures.get("icon"), letter=title[:1], chips=chips)
    unit_name = data.get("unit", "hours")
    rows = data["rows"]
    height = 90 + 62 * len(rows)
    for box, (title_text, icon, value_of) in zip(_columns_of(3, top, height), (
        (words.t(lang, "messages"), "hash", lambda row: _count(lang, row["messages"], "message")),
        (words.t(lang, "voice"), "speaker", lambda row: _voice(lang, row["voice"], unit_name)),
        (words.t(lang, "contributors"), "people", lambda row: _count(lang, row["people"], "member")),
    )):
        inner = d.panel(card, box, title_text, icon)
        d.rows_area(card, inner, [(_row_label(lang, row["days"], data), *value_of(row)) for row in rows])
    second = top + height + G
    left_box, right_box = _columns_of(2, second, 190)
    inner = d.panel(card, left_box, words.t(lang, "top_members"), "trophy")
    people_rows = []
    for icon, entry, value in (("hash", data.get("top_member_messages"), lambda e: words.figure(lang, e["value"])),
                               ("speaker", data.get("top_member_voice"), lambda e: _voice_short(lang, e["value"], unit_name))):
        label, plain = _person_label(lang, entry)
        people_rows.append({"label": label, "plain": plain, "value_text": value(entry) if entry else "", "icon": icon})
    d.name_value_rows(card, inner, people_rows, marker=_icon_marker)
    inner = d.panel(card, right_box, words.t(lang, "top_channels"), "hash")
    channel_rows = []
    for icon, entry, value in (("hash", data.get("top_channel_messages"), lambda e: words.figure(lang, e["value"])),
                               ("speaker", data.get("top_channel_voice"), lambda e: _voice_short(lang, e["value"], unit_name))):
        label, plain = _channel_label(lang, entry, mark=False)  # the row's icon already says text or voice
        channel_rows.append({"label": label, "plain": plain, "value_text": value(entry) if entry else "", "icon": icon})
    d.name_value_rows(card, inner, channel_rows, marker=_icon_marker)
    third = second + 190 + G
    _mini_pair(card, (M, third, W - M, third + 250), lang, data, data["chart"])
    return _finish(card, third + 250, data)


# ---- the member card ----

def member(data: dict, lang: str, pictures: dict) -> bytes:
    card = d.Card(TALL)
    chips = []
    if data.get("created"):
        chips.append((words.t(lang, "account_created"), words.date(lang, data["created"], data["tz"])))
    if data.get("joined") and data.get("in_server", True):
        chips.append((words.t(lang, "joined"), words.date(lang, data["joined"], data["tz"])))
    title = person_name(lang, data)
    username = d.readable(data.get("username")) or ""
    subtitle = " · ".join(part for part in (("@" + username) if username and username != title else "", d.drawable(data.get("server") or "", "")) if part)
    top = d.header(card, title=title, subtitle=subtitle, picture=pictures.get("avatar"), letter=title[:1], chips=chips)
    unit_name = data.get("unit", "hours")
    rows = data["rows"]
    height = 90 + 62 * max(len(rows), 2)
    boxes = _columns_of(3, top, height)
    inner = d.panel(card, boxes[0], words.t(lang, "ranks"), "trophy")
    rank_rows = []
    for key in ("messages", "voice"):
        rank, count = data["ranks"][key]
        rank_rows.append((words.t(lang, key), words.t(lang, "rank_short", rank=words.number(lang, rank)) if rank else words.t(lang, "not_ranked"),
                          words.t(lang, "of_total", total=words.number(lang, count)) if rank else ""))
    d.rows_area(card, inner, rank_rows, label_width=150, size=30)
    inner = d.panel(card, boxes[1], words.t(lang, "messages"), "hash")
    d.rows_area(card, inner, [(_row_label(lang, row["days"], data), *_count(lang, row["messages"], "message")) for row in rows])
    inner = d.panel(card, boxes[2], words.t(lang, "voice"), "speaker")
    d.rows_area(card, inner, [(_row_label(lang, row["days"], data), *_voice(lang, row["voice"], unit_name)) for row in rows])
    second = top + height + G
    level = data.get("level")
    parts = _split((M, second, W - M, second + 250), (2.1, 1) if level else (1,))
    inner = d.panel(card, parts[0], words.t(lang, "at_a_glance"), "chart")
    _facts_grid(card, inner, lang, data, pictures, unit_name)
    if level:
        _level_panel(card, parts[1], lang, level)
    third = second + 250 + G
    _mini_pair(card, (M, third, W - M, third + 250), lang, data, data["chart"])
    return _finish(card, third + 250, data)


def _facts_grid(card: d.Card, box: Sequence[float], lang: str, data: dict, pictures: dict, unit_name: str) -> None:
    """The member's top text channel, top voice channel, most used app and most used emoji, two by two."""
    text_channel = (data.get("text_channels") or [None])[0]
    voice_channel = data.get("voice_channel")
    app = data.get("app")
    emoji = data.get("emoji")
    items = [
        ("hash", words.t(lang, "top_text_channel"), *_channel_label(lang, text_channel), words.figure(lang, text_channel["value"]) if text_channel else "", None),
        ("speaker", words.t(lang, "top_voice_channel"), *_channel_label(lang, voice_channel), _voice_short(lang, voice_channel["value"], unit_name) if voice_channel else "", None),
        ("gamepad", words.t(lang, "most_used_app"), d.drawable((app or {}).get("name") or "", EMPTY), bool(app), _voice_short(lang, app["seconds"], unit_name) if app else "", None),
        ("smile", words.t(lang, "most_used_emoji"), _emoji_label(lang, emoji) if emoji else EMPTY, bool(emoji), words.figure(lang, emoji["value"]) if emoji else "", emoji),
    ]
    for cell, (icon, caption, name, plain, value, emoji_entry) in zip(_grid(box, 2, 2), items):
        x0, y0, x1, y1 = cell
        card.rect(cell, d.WELL, 10)
        card.icon(icon, x0 + 26, y0 + 26, 22, d.DIM)
        card.fitted(x0 + 48, y0 + 34, caption, x1 - x0 - 60, 19, color=d.DIM, minimum=14)
        middle = y1 - (y1 - y0) * 0.3
        left = x0 + 16
        if emoji_entry is not None:
            left += _emoji_marker(pictures)(card, emoji_entry, left, middle, (y1 - y0) * 0.5)
        value_width = card.text(x1 - 16, middle, value, 24, bold=True, anchor="rm") if value else 0
        card.fitted(left, middle, name, x1 - 16 - value_width - 16 - left, 26, color=d.TEXT if plain else d.DIM, minimum=21, anchor="lm")


def _level_panel(card: d.Card, box: Sequence[float], lang: str, level: dict) -> None:
    x0, y0, x1, y1 = d.panel(card, box, words.t(lang, "level"), "star")
    card.text(x0, y0 + 52, words.number(lang, level["level"]), 56, bold=True)
    if level.get("rank"):
        card.text(x1, y0 + 52, words.t(lang, "rank_short", rank=words.number(lang, level["rank"])), 30, bold=True, color=d.SOFT, anchor="rs")
    fraction = max(0.0, min(1.0, level["into"] / level["size"])) if level.get("size") else 0.0
    bar_top = y0 + 76
    card.rect((x0, bar_top, x1, bar_top + 20), d.WELL, 10)
    if fraction > 0:
        card.rect((x0, bar_top, x0 + max(20, (x1 - x0) * fraction), bar_top + 20), d.SERIES[0], 10)
    card.fitted(x0, y1 - 6, f"{words.number(lang, level['into'])} / {words.number(lang, level['size'])} {words.t(lang, 'xp')}", x1 - x0, 22, color=d.SOFT, minimum=15)


# ---- the channel card ----

def channel(data: dict, lang: str, pictures: dict) -> bytes:
    card = d.Card(TALL)
    has_voice = "voice" in data["blocks"]
    icon = {"voice": "speaker", "stage": "speaker", "category": "folder", "merged": "folder"}.get(data.get("kind"), "hash")
    chips = [(words.t(lang, "created"), words.date(lang, data["created"], data["tz"]))] if data.get("created") else []
    title_key = "category_overview" if data.get("kind") == "category" else "channel_overview"
    subtitle = " · ".join(part for part in (words.t(lang, title_key), d.drawable(data.get("server") or "", "")) if part)
    title = d.drawable(data["title"], "#")
    if data.get("kind") in ("text", "forum"):
        title = title.lstrip("#")
    top = d.header(card, title=title, subtitle=subtitle, icon=icon, chips=chips)
    unit_name = data.get("unit", "hours")
    message_rows = data["blocks"]["messages"]
    labels = [_row_label(lang, row["days"], data) for row in message_rows]
    panels = [(words.t(lang, "messages"), "hash", [(label, *_count(lang, row["total"], "message")) for label, row in zip(labels, message_rows)]),
              (words.t(lang, "contributors"), "people", [(label, *_count(lang, row["people"], "member")) for label, row in zip(labels, message_rows)])]
    if has_voice:
        panels.append((words.t(lang, "voice"), "speaker", [(label, *_voice(lang, row["total"], unit_name)) for label, row in zip(labels, data["blocks"]["voice"])]))
    height = 90 + 62 * len(message_rows)
    boxes = _columns_of(3, top, height)
    for box, (title_text, icon_name, rows) in zip(boxes, panels):
        inner = d.panel(card, box, title_text, icon_name)
        d.rows_area(card, inner, rows, size=28)
    emoji_rows = [{**entry, "label": _emoji_label(lang, entry), "value_text": words.figure(lang, entry["value"])} for entry in data.get("top_emoji") or []]
    if not has_voice:
        inner = d.panel(card, boxes[2], words.t(lang, "top_emoji"), "smile")
        d.name_value_rows(card, _fill(inner, len(emoji_rows), 3), emoji_rows, marker=_emoji_marker(pictures), empty=words.t(lang, "nothing_yet"))
    member_rows = []
    for entry in data.get("top_members") or []:
        label, plain = _person_label(lang, entry)
        member_rows.append({"label": label, "plain": plain, "value_text": words.figure(lang, entry["value"])})
    second = top + height + G
    left_box, right_box = _columns_of(2, second, 250)
    inner = d.panel(card, left_box, words.t(lang, "top_members"), "trophy")
    d.name_value_rows(card, _fill(inner, len(member_rows), 3), member_rows, rank=True, empty=words.t(lang, "nothing_yet"))
    if has_voice:
        inner = d.panel(card, right_box, words.t(lang, "top_emoji"), "smile")
        d.name_value_rows(card, _fill(inner, len(emoji_rows), 3), emoji_rows, marker=_emoji_marker(pictures), empty=words.t(lang, "nothing_yet"))
        third = second + 250 + G
        _mini_pair(card, (M, third, W - M, third + 250), lang, data, data["chart"])
        return _finish(card, third + 250, data)
    _mini_pair(card, right_box, lang, data, data["chart"], voice=False)
    return _finish(card, second + 250, data)


def _fill(box: Sequence[float], rows: int, slots: int) -> tuple[float, float, float, float]:
    """The part of a box that `rows` rows take when it has room for `slots`, so a short list keeps its rows' height."""
    x0, y0, x1, y1 = box
    if not rows or rows >= slots:
        return x0, y0, x1, y1
    return x0, y0, x1, y0 + (y1 - y0 + 10) * rows / slots - 10


# ---- the messages and voice cards ----

def totals(data: dict, lang: str, pictures: dict) -> bytes:
    card = d.Card(TALL)
    voice = data["kind"] == "voice"
    unit_name = data.get("unit", "hours")
    top = d.header(card, title=words.t(lang, "voice_overview" if voice else "message_overview"), subtitle=d.drawable(data["title"], ""),
                   icon="speaker" if voice else "hash")
    value_of = (lambda value: _voice(lang, value, unit_name)) if voice else (lambda value: _count(lang, value, "message"))
    boxes = _columns_of(3, top, 290)
    period = data["rows"][-1]
    x0, y0, x1, y1 = d.panel(card, boxes[0], words.t(lang, "period_total"), "chart")
    value, unit_text = value_of(period["total"])[:2]
    card.fitted((x0 + x1) / 2, (y0 + y1) / 2 + 10, value, x1 - x0, 76, bold=True, minimum=36, anchor="ms")
    card.fitted((x0 + x1) / 2, (y0 + y1) / 2 + 54, unit_text, x1 - x0, 30, color=d.SOFT, minimum=18, anchor="ms")
    card.fitted((x0 + x1) / 2, y1 - 4, _short_period(data), x1 - x0, 22, color=d.DIM, minimum=14, anchor="ms")
    inner = d.panel(card, boxes[1], words.t(lang, "voice" if voice else "messages"), "speaker" if voice else "hash")
    d.rows_area(card, inner, [(_row_label(lang, row["days"], data), *value_of(row["total"])) for row in data["rows"][:3]])
    # The other kind beside it: voice time on the messages card, messages on the voice card
    other_of = (lambda value: _count(lang, value, "message")) if voice else (lambda value: _voice(lang, value, unit_name))
    inner = d.panel(card, boxes[2], words.t(lang, "messages" if voice else "voice"), "hash" if voice else "speaker")
    d.rows_area(card, inner, [(_row_label(lang, row["days"], data), *other_of(row["total"])) for row in data.get("other_rows") or []])
    second = top + 290 + G
    left_box, right_box = _columns_of(2, second, 280)
    fmt = (lambda v: _voice_short(lang, v, unit_name)) if voice else (lambda v: words.figure(lang, v))
    rows = []
    for entry in data["top_members"]:
        label, plain = _person_label(lang, entry)
        rows.append({"label": label, "plain": plain, "value_text": fmt(entry["value"])})
    inner = d.panel(card, left_box, words.t(lang, "top_members"), "trophy")
    d.name_value_rows(card, _fill(inner, len(rows), 3), rows, rank=True, empty=words.t(lang, "nothing_yet"))
    rows = []
    for entry in data["top_channels"]:
        label, plain = _channel_label(lang, entry)
        rows.append({"label": label, "plain": plain, "value_text": fmt(entry["value"])})
    inner = d.panel(card, right_box, words.t(lang, "top_channels"), "speaker" if voice else "hash")
    d.name_value_rows(card, _fill(inner, len(rows), 3), rows, rank=True, empty=words.t(lang, "nothing_yet"))
    return _finish(card, second + 280, data)


# ---- top tens beside a ring: activities and emoji ----

def _ranked_with_ring(card: d.Card, top: float, lang: str, entries: list[dict], total: float, value_text: Callable[[float], str],
                      total_text: tuple[str, str], marker: Callable | None, title: str, icon: str, empty: str) -> float:
    left_box, right_box = _split((M, top, W - M, top + 640), (1.45, 1))
    inner = d.panel(card, left_box, title, icon)
    swatches = [d.SERIES[index] if index < DONUT_PARTS else d.OTHER for index in range(len(entries))]
    rows = [{**entry, "rank": index + 1, "value_text": value_text(entry["value"])} for index, entry in enumerate(entries)]
    if rows:
        d.name_value_rows(card, _fill(inner, len(rows), 10), rows, rank=True, swatches=swatches, marker=marker, size=26)
    else:
        card.fitted((inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2, empty, inner[2] - inner[0], 22, color=d.DIM, minimum=14, anchor="mm")
    x0, y0, x1, y1 = d.panel(card, right_box, words.t(lang, "total"), "chart")
    radius = min(x1 - x0, y1 - y0 - 30) / 2 - 6
    center = ((x0 + x1) / 2, y0 + (y1 - y0 - 30) / 2)
    shown = entries[:DONUT_PARTS]
    rest = max(0.0, total - sum(entry["value"] for entry in shown))
    parts = [(entry["value"], d.SERIES[index]) for index, entry in enumerate(shown)] + ([(rest, d.OTHER)] if rest > 0 else [])
    d.donut(card, center, radius, radius * 0.34, parts)
    card.fitted(center[0], center[1] + 6, total_text[0], radius * 1.2, 44, bold=True, minimum=22, anchor="ms")
    card.fitted(center[0], center[1] + 42, total_text[1], radius * 1.2, 24, color=d.SOFT, minimum=14, anchor="ms")
    if rest > 0 and shown:
        card.swatch(x0 + 8, y1 - 10, d.OTHER, 16)
        card.text(x0 + 26, y1 - 10, words.t(lang, "other"), 20, color=d.SOFT, anchor="lm")
    return top + 640


def activity(data: dict, lang: str, pictures: dict) -> bytes:
    card = d.Card(TALL)
    top = d.header(card, title=words.t(lang, "activity_overview"), subtitle=d.drawable(data["title"], ""), icon="gamepad")
    unit_name = data.get("unit", "hours")
    verbs = {0: "playing", 1: "streaming", 2: "listening", 3: "watching", 5: "competing"}
    entries = [{**entry, "label": d.drawable(entry["name"], "?"), "plain": True, "verb": words.t(lang, verbs.get(entry.get("kind"), "playing"))} for entry in data["top"]]
    total_value, total_unit = _voice(lang, data["total"], unit_name)[:2]
    bottom = _ranked_with_ring(card, top, lang, entries, data["total"], lambda v: _voice_short(lang, v, unit_name), (total_value, total_unit), None,
                               words.t(lang, "top_activities"), "gamepad", words.t(lang, "no_activity_data"))
    return _finish(card, bottom, data)


def emoji(data: dict, lang: str, pictures: dict) -> bytes:
    card = d.Card(TALL)
    top = d.header(card, title=words.t(lang, "emoji_overview"), subtitle=d.drawable(data["title"], ""), icon="smile")
    entries = [{**entry, "label": _emoji_label(lang, entry), "plain": True} for entry in data["top"]]
    shown = words.figure(lang, data["total"])
    bottom = _ranked_with_ring(card, top, lang, entries, data["total"], lambda v: words.figure(lang, v), (shown, words.unit(lang, "use", data["total"], shown)),
                               _emoji_marker(pictures), words.t(lang, "top_emoji"), "smile", words.t(lang, "nothing_yet"))
    return _finish(card, bottom, data)


# ---- invites ----

def _invite_chips(lang: str, data: dict) -> list[tuple[str, str]]:
    chips = [(words.t(lang, "counted_joins"), words.figure(lang, data["joins"]))]
    if data.get("fake"):
        chips.append((words.t(lang, "fake_left_out", count="").strip(), words.figure(lang, data["fake"])))
    return chips


def invites(data: dict, lang: str, pictures: dict) -> bytes:
    card = d.Card(TALL)
    top = d.header(card, title=words.t(lang, "invite_overview"), subtitle=d.drawable(data["title"], ""), icon="mail", chips=_invite_chips(lang, data))
    left_box, right_box = _columns_of(2, top, 400)
    rows = []
    for entry in data["top_invites"]:
        label, plain = _invite_label(lang, entry)
        rows.append({"label": label, "plain": plain, "value_text": words.figure(lang, entry["value"])})
    inner = d.panel(card, left_box, words.t(lang, "top_invites"), "mail")
    empty = words.t(lang, "nothing_yet") if data.get("can_read", True) else words.t(lang, "check_invites")
    d.name_value_rows(card, _fill(inner, len(rows), 5), rows, rank=True, empty=empty)
    rows = []
    for entry in data["top_inviters"]:
        label, plain = _person_label(lang, entry)
        rows.append({"label": label, "plain": plain, "value_text": words.figure(lang, entry["value"])})
    inner = d.panel(card, right_box, words.t(lang, "top_inviters"), "trophy")
    d.name_value_rows(card, _fill(inner, len(rows), 5), rows, rank=True, empty=words.t(lang, "nothing_yet"))
    second = top + 400 + G
    inner = d.panel(card, (M, second, W - M, second + 250))
    chart = data["chart"]
    peak = max(chart["joins"], default=0)
    _measure(card, inner, chart["joins"], d.SERIES[0], _labels(lang, chart, data["tz"]), _count_axis(lang),
             title=words.t(lang, "chart_of", what=words.t(lang, "counted_joins"), per=_per(lang, chart["interval"])),
             note=words.t(lang, "peak", value=words.figure(lang, peak)) if peak else "", empty=words.t(lang, "nothing_yet"), limit=MINI_COLUMNS, title_size=22)
    return _finish(card, second + 250, data)


# ---- chart cards ----

CHART_HEIGHT = 520  # the panel below a chart card's header


def _chart_frame(lang: str, data: dict, *, title: str, icon: str, chips: Sequence[tuple[str, str]] = ()) -> tuple[d.Card, tuple[float, float, float, float]]:
    """A chart card's header and the inside of its one big panel."""
    card = d.Card(TALL)
    subtitle = d.drawable(data.get("server") or data.get("title") or "", "")
    if data.get("server"):
        about = person_name(lang, data) if data.get("view") == "chart_member" else d.drawable(data["title"], "")
        title = f"{title} · {about}"
    top = d.header(card, title=title, subtitle=subtitle, icon=icon, chips=chips)
    inner = d.panel(card, (M, top, W - M, top + CHART_HEIGHT))
    card.chart_bottom = top + CHART_HEIGHT
    return card, inner


def _two(inner: Sequence[float], share: float = 0.5) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    x0, y0, x1, y1 = inner
    middle = y0 + (y1 - y0) * share
    return (x0, y0, x1, middle - 10), (x0, middle + 10, x1, y1)


def chart_kind(data: dict, lang: str, pictures: dict) -> bytes:
    voice = data["kind"] == "voice"
    unit_name = data.get("unit", "hours")
    total = sum(data["totals"])
    chips = [(words.t(lang, "voice" if voice else "messages"), _voice_short(lang, total, unit_name) if voice else words.compact(lang, total))]
    card, inner = _chart_frame(lang, data, title=words.t(lang, "voice" if voice else "messages"), icon="speaker" if voice else "hash", chips=chips)
    labels = _labels(lang, data, data["tz"])
    per = _per(lang, data["interval"])
    upper, lower = _two(inner, 0.56)
    values = _in_unit(data["totals"], unit_name) if voice else data["totals"]
    what = words.t(lang, "voice" if voice else "messages")
    _measure(card, upper, values, d.SERIES[0], labels, _count_axis(lang), title=words.t(lang, "chart_of", what=what, per=per),
             note=words.short_unit(lang, unit_name) if voice else "", empty=words.t(lang, "nothing_yet"))
    _measure(card, lower, data["people"], d.SERIES[1], labels, _count_axis(lang), title=words.t(lang, "chart_of", what=words.t(lang, "contributors"), per=per))
    return _finish(card, card.chart_bottom, data)


def chart_members(data: dict, lang: str, pictures: dict) -> bytes:
    chips = []
    if data.get("now"):
        chips.append((words.t(lang, "members_now"), words.number(lang, data["now"])))
    chips.append((words.t(lang, "joins"), words.number(lang, data["total_joins"])))
    chips.append((words.t(lang, "leaves"), words.number(lang, data["total_leaves"])))
    card, inner = _chart_frame(lang, data, title=words.t(lang, "members_chart"), icon="people", chips=chips)
    labels = _labels(lang, data, data["tz"])
    upper, lower = _two(inner, 0.55)
    x0, y0, x1, y1 = upper
    card.text(x0, y0 + 24, words.t(lang, "members_chart"), 23, bold=True, color=d.SOFT)
    members = data["members"]
    known = [value for value in members if value is not None]
    if known:
        low, high = min(known), max(known)
        span = max(high - low, 1)
        step = d.nice_step(span * 1.3, 4)
        offset = max(0.0, (low - span * 0.15) // step * step)  # the axis starts near the lowest count, at a round number, so growth shows
        shifted = [None if value is None else value - offset for value in members]
        plot = d.Plot(card, (x0, y0 + 36, x1, y1), max(value for value in shifted if value is not None), labels, lambda value: words.compact(lang, value + offset))
        d.area(plot, shifted, d.SERIES[0], partial=True)
    else:
        _nothing(card, d.Plot(card, (x0, y0 + 36, x1, y1), 0, labels, _count_axis(lang)), words.t(lang, "nothing_yet"))
    x0, y0, x1, y1 = lower
    card.text(x0, y0 + 24, words.t(lang, "chart_of", what=words.t(lang, "joins_and_leaves"), per=_per(lang, data["interval"])), 23, bold=True, color=d.SOFT)
    d.legend(card, x1, y0 + 16, [(words.t(lang, "joins"), d.UP), (words.t(lang, "leaves"), d.DOWN)], right=True)
    bars = len(labels) <= COLUMN_LIMIT
    plot = d.Plot(card, (x0, y0 + 36, x1, y1), max(data["joins"], default=0), labels, _count_axis(lang), bottom_value=-max(max(data["leaves"], default=0), 1),
                  columns=bars)
    if bars:
        d.diverging_columns(plot, data["joins"], data["leaves"], partial=True)
    else:
        d.area(plot, data["joins"], d.UP, partial=True)
        d.area(plot, [-value for value in data["leaves"]], d.DOWN, partial=True)
    return _finish(card, card.chart_bottom, data)


def chart_status(data: dict, lang: str, pictures: dict) -> bytes:
    card, inner = _chart_frame(lang, data, title=words.t(lang, "statuses"), icon="people")
    labels = _labels(lang, data, data["tz"])
    x0, y0, x1, y1 = inner
    names = ("online", "idle", "dnd")
    if data["breakdown"]:
        layers = [([value or 0 for value in data[name]], d.SERIES[index]) for index, name in enumerate(names)]
        d.legend(card, x1, y0 + 16, [(words.t(lang, name), d.SERIES[index]) for index, name in enumerate(names)], right=True)
        sums = [sum(layer[0][index] for layer in layers) for index in range(len(labels))]
        plot = d.Plot(card, (x0, y0 + 40, x1, y1), max(sums, default=0), labels, _count_axis(lang))
        if any(sums):
            d.stacked_area(plot, layers)
        else:
            _nothing(card, plot, words.t(lang, "nothing_yet"))
    else:
        card.text(x0, y0 + 24, words.t(lang, "not_offline"), 23, bold=True, color=d.SOFT)
        card.fitted(x0, y0 + 54, words.t(lang, "status_estimate"), x1 - x0, 19, color=d.DIM, minimum=13)
        values = data["present"]
        plot = d.Plot(card, (x0, y0 + 68, x1, y1), max((value or 0 for value in values), default=0), labels, _count_axis(lang))
        if any(values):
            d.area(plot, values, d.SERIES[0], partial=True)
        else:
            _nothing(card, plot, words.t(lang, "nothing_yet"))
    return _finish(card, card.chart_bottom, data)


def _lines_chart(card: d.Card, inner: Sequence[float], lang: str, data: dict, names: list[str], values: list[list[float]], empty: str) -> None:
    labels = _labels(lang, data, data["tz"])
    x0, y0, x1, y1 = inner
    if names:
        d.legend(card, x0, y0 + 16, [(name, d.SERIES[index]) for index, name in enumerate(names)], room=x1 - x0)
    peak = max((value for line in values for value in line), default=0)
    plot = d.Plot(card, (x0, y0 + 40, x1, y1), peak, labels, _count_axis(lang))
    if not peak:
        _nothing(card, plot, empty)
        return
    d.lines(plot, [(line, d.SERIES[index]) for index, line in enumerate(values)], partial=True)


def chart_activity(data: dict, lang: str, pictures: dict) -> bytes:
    card, inner = _chart_frame(lang, data, title=words.t(lang, "activities_chart"), icon="gamepad")
    unit_name = data.get("unit", "hours")
    _lines_chart(card, inner, lang, data, [d.drawable(line["name"], "?") for line in data["lines"]],
                 [_in_unit(line["values"], unit_name) for line in data["lines"]], words.t(lang, "no_activity_data"))
    return _finish(card, card.chart_bottom, data)


def chart_emoji(data: dict, lang: str, pictures: dict) -> bytes:
    card, inner = _chart_frame(lang, data, title=words.t(lang, "emoji_chart"), icon="smile")
    _lines_chart(card, inner, lang, data, [_emoji_label(lang, line) for line in data["lines"]], [line["values"] for line in data["lines"]],
                 words.t(lang, "nothing_yet"))
    return _finish(card, card.chart_bottom, data)


def chart_invites(data: dict, lang: str, pictures: dict) -> bytes:
    card, inner = _chart_frame(lang, data, title=words.t(lang, "invites_chart"), icon="mail", chips=_invite_chips(lang, data))
    labels = _labels(lang, data, data["tz"])
    x0, y0, x1, y1 = inner
    layers = [(stack["values"], d.SERIES[index]) for index, stack in enumerate(data["stacks"])]
    names = [_invite_label(lang, stack)[0] for stack in data["stacks"]]
    if any(data["other"]):
        layers.append((data["other"], d.OTHER))
        names.append(words.t(lang, "other"))
    if layers:
        d.legend(card, x0, y0 + 16, [(name, color) for name, (_, color) in zip(names, layers)], room=x1 - x0)
    sums = [sum(values[index] for values, _ in layers) for index in range(len(labels))]
    bars = len(labels) <= COLUMN_LIMIT
    plot = d.Plot(card, (x0, y0 + 40, x1, y1), max(sums, default=0), labels, _count_axis(lang), columns=bars)
    if not any(sums):
        _nothing(card, plot, words.t(lang, "nothing_yet"))
    elif bars:
        d.stacked_columns(plot, layers)
    else:
        d.stacked_area(plot, layers)
    return _finish(card, card.chart_bottom, data)


def chart_member(data: dict, lang: str, pictures: dict) -> bytes:
    unit_name = data.get("unit", "hours")
    chips = [(words.t(lang, "messages"), words.compact(lang, sum(data["messages"]))), (words.t(lang, "voice"), _voice_short(lang, sum(data["voice"]), unit_name))]
    card, inner = _chart_frame(lang, data, title=words.t(lang, "member_overview"), icon="person", chips=chips)
    labels = _labels(lang, data, data["tz"])
    per = _per(lang, data["interval"])
    upper, lower = _two(inner)
    _measure(card, upper, data["messages"], d.SERIES[0], labels, _count_axis(lang), title=words.t(lang, "chart_of", what=words.t(lang, "messages"), per=per),
             empty=words.t(lang, "nothing_yet"))
    _measure(card, lower, _in_unit(data["voice"], unit_name), d.SERIES[1], labels, _count_axis(lang),
             title=words.t(lang, "chart_of", what=words.t(lang, "voice"), per=per), note=words.short_unit(lang, unit_name), empty=words.t(lang, "nothing_yet"))
    return _finish(card, card.chart_bottom, data)


def chart_channel(data: dict, lang: str, pictures: dict) -> bytes:
    unit_name = data.get("unit", "hours")
    voice = "voice" in data
    chips = [(words.t(lang, "messages"), words.compact(lang, sum(data["messages"])))]
    if voice:
        chips.append((words.t(lang, "voice"), _voice_short(lang, sum(data["voice"]), unit_name)))
    icon = {"voice": "speaker", "stage": "speaker", "category": "folder"}.get(data.get("kind"), "hash")
    title_key = "category_overview" if data.get("kind") == "category" else "channel_overview"
    card, inner = _chart_frame(lang, data, title=words.t(lang, title_key), icon=icon, chips=chips)
    labels = _labels(lang, data, data["tz"])
    per = _per(lang, data["interval"])
    upper, lower = _two(inner)
    _measure(card, upper, data["messages"], d.SERIES[0], labels, _count_axis(lang), title=words.t(lang, "chart_of", what=words.t(lang, "messages"), per=per),
             empty=words.t(lang, "nothing_yet"))
    if voice:
        _measure(card, lower, _in_unit(data["voice"], unit_name), d.SERIES[1], labels, _count_axis(lang),
                 title=words.t(lang, "chart_of", what=words.t(lang, "voice"), per=per), note=words.short_unit(lang, unit_name), empty=words.t(lang, "nothing_yet"))
    else:
        _measure(card, lower, data["people"], d.SERIES[1], labels, _count_axis(lang), title=words.t(lang, "chart_of", what=words.t(lang, "contributors"), per=per))
    return _finish(card, card.chart_bottom, data)


# ---- /top ----

TOP_TITLES = {"overview": "show_overview", "members_messages": "top_message_members", "members_voice": "top_voice_members",
              "channels_messages": "top_message_channels", "channels_voice": "top_voice_channels", "emoji": "top_emoji"}


def _row_text(lang: str, kind: str, entry: dict) -> dict:
    if kind == "person":
        label, plain = _person_label(lang, entry)
    elif kind == "channel":
        label, plain = _channel_label(lang, entry)
    else:
        label, plain = _emoji_label(lang, entry), True
    return {"label": label, "plain": plain}


def top_card(data: dict, lang: str, pictures: dict) -> bytes:
    page_kind = data["page_kind"]
    card = d.Card(TALL)
    chips = []
    if page_kind != "overview":
        chips.append((words.t(lang, "ranked_count", count=words.number(lang, data.get("count", 0))),
                      words.t(lang, "page_of", page=words.number(lang, data["page"] + 1), pages=words.number(lang, data["pages"]))))
    title = d.drawable(data["title"], "Server")
    top = d.header(card, title=title, subtitle=words.t(lang, "top_overview") + " · " + words.t(lang, TOP_TITLES[page_kind]), picture=pictures.get("icon"),
                   letter=title[:1], chips=chips)
    unit_name = data.get("unit", "hours")

    def voice_value(value: float) -> str:
        return _voice_short(lang, value, unit_name)

    def count_value(value: float) -> str:
        return words.figure(lang, value)

    if page_kind == "overview":
        sections = data["sections"]
        first = _columns_of(3, top, 270)
        second = _columns_of(2, top + 270 + G, 270)
        layout = [
            (first[0], "top_message_members", "hash", sections["members_messages"], "person", count_value),
            (first[1], "top_voice_members", "speaker", sections["members_voice"], "person", voice_value),
            (first[2], "top_emoji", "smile", sections["emoji"], "emoji", count_value),
            (second[0], "top_message_channels", "hash", sections["channels_messages"], "channel", count_value),
            (second[1], "top_voice_channels", "speaker", sections["channels_voice"], "channel", voice_value),
        ]
        for box, title_key, icon, entries, kind, fmt in layout:
            inner = d.panel(card, box, words.t(lang, title_key), icon)
            rows = [{**entry, **_row_text(lang, kind, entry), "value_text": fmt(entry["value"]), "rank": index + 1} for index, entry in enumerate(entries)]
            d.name_value_rows(card, _fill(inner, len(rows), 3), rows, rank=True, size=25, marker=_emoji_marker(pictures) if kind == "emoji" else None,
                              empty=words.t(lang, "nothing_yet"))
        return _finish(card, top + 270 + G + 270, data)
    kind = "person" if page_kind.startswith("members") else "channel" if page_kind.startswith("channels") else "emoji"
    fmt = voice_value if page_kind.endswith("voice") else count_value
    rows = [{**entry, **_row_text(lang, kind, entry), "value_text": fmt(entry["value"])} for entry in data["rows"]]
    height = 5 * 74 + 4 * 10 + 2 * d.PAD
    for index, (box, part) in enumerate(zip(_columns_of(2, top, height), (rows[:5], rows[5:10]))):
        inner = d.panel(card, box)
        if part:
            d.name_value_rows(card, _fill(inner, len(part), 5), part, rank=True, size=27, marker=_emoji_marker(pictures) if kind == "emoji" else None)
        elif index == 0:
            card.text((inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2, words.t(lang, "nothing_yet"), 22, color=d.DIM, anchor="mm")
    return _finish(card, top + height, data)


RENDERERS: dict[str, Callable[[dict, str, dict], bytes]] = {
    "server": server, "member": member, "channel": channel, "messages": totals, "voice": totals, "activity": activity, "emoji": emoji,
    "invites": invites, "chart_messages": chart_kind, "chart_voice": chart_kind, "chart_members": chart_members, "chart_status": chart_status,
    "chart_activity": chart_activity, "chart_emoji": chart_emoji, "chart_invites": chart_invites, "chart_member": chart_member,
    "chart_channel": chart_channel, "top": top_card,
}


_DRAWING = threading.Lock()  # one card at a time: the cached fonts are shared, and nothing promises a font is safe in two threads at once


def render(data: dict, lang: str, pictures: dict | None = None) -> bytes:
    """The card for what views.gather returned, as PNG bytes."""
    with _DRAWING:
        return RENDERERS[data["view"]](data, lang, pictures or {})
