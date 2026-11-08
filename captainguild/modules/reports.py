"""Member reports, the review thread staff keep for each reported member, and the moderation tools that go with them.

A report is a post in the reports channel and a row here. The row decides what the post says, so a button, the dashboard and the
review checklist can each change a report without reading its text back.

A review thread holds at most one checklist message. Every rule below answers a failure seen on a live server:

- A checklist is posted only when a review starts, when a step moves on, or when the message the thread owns was really deleted.
  Who spoke last in the thread has no bearing on it. Polling for the last word is what once flooded threads with copies.
- One lock per thread covers every write, so two moderators clicking together can't each post a copy.
- A replacement is posted before the old message is deleted, so a failed post never leaves a thread with no checklist.
- A failed lookup is not a missing message. Only a clean 404 counts as gone.
- Finishing deletes the checklist and posts nothing, because a notice would sit in the thread with no way to clear it.
- Dismissing is permanent until a new report reopens the review. A new report on a review still running keeps its step, because a flood of reports must not throw a moderator back to step one.
- Nobody works the report or the review about themselves, unless they're the owner or an Administrator, who have no one above them.
- Only `_begin_review` creates a state row. A write already in flight can't bring back a review that has closed.
- A copy that isn't the tracked message deletes itself when clicked.
- Background work never writes to an archived thread, because that would unarchive it and bump the forum.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core import access, delivery
from ..core.messages import component_view, unsigned
from ..core.module import Module, requires, staff_only
from ..core.ops import MessageGone, OpsError, upload_limit
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.tagscript import RegexGuard
from ..core.usage import plain_error
from ..core.validate import Field, Invalid, Schema, boolean, choice, integer, list_of, optional, page_number, snowflake, text, whole_number
from ..web.api import ApiError

log = logging.getLogger(__name__)

OPEN, ACTIONED, FALSE_POSITIVE = "open", "actioned", "false_positive"
STATES = (OPEN, ACTIONED, FALSE_POSITIVE)
STATE_COLORS = {OPEN: 0x2F3136, ACTIONED: 0x2ECC71, FALSE_POSITIVE: 0x992D22}

REPORT_ACTIONED_ID = "report:actioned"
REPORT_FALSE_ID = "report:false"
REPORT_HISTORY_ID = "report:history"
REVIEW_START_ID = "review:start"
REVIEW_DONE_ID = "review:completed"
REVIEW_DISMISS_ID = "review:dismiss"

ACTIVE, DONE, DISMISSED = "active", "done", "dismissed"

# The checklist. An index here is the step number kept in review_state.
REVIEW_STEPS = [
    "Gather the user's recent messages and logs.",
    "Review previous mutes or warns and propose an action.",
    "Create a ticket to discuss the situation with the user.",
    "Acknowledge the user report in the reported messages channel.",
    "Process completed.",
]
STEP_BUTTONS = ["Gather Recent Messages & Logs", "Propose Action", "Create Ticket", "Acknowledge Report", "Process Completed"]
UID_IN_NAME = re.compile(r"UID (\d+)")
STEP_TITLE = re.compile(r"^Step [1-5]$")

SCAN_PER_CHANNEL = 100  # how far back each channel is read when the message store falls short
SCAN_BATCH = 5  # channels read at the same time, small enough to stay under Discord's rate limit
SCAN_PAUSE = 2.0
POST_PAUSE = 0.5
ROLE_PAUSE = 0.3
DELETE_PAUSE = 0.4
GATHER_MAX = 1000  # the most the log channel scan can return
LOG_CARD_SCAN = 500  # how far back the older log channel is read for one reported message
HISTORY_LIMIT = 50
FILES_PER_MESSAGE = 10
TOTAL_UPLOAD_BYTES = 24 * 1024 * 1024
REPORT_CONTENT_MAX = 1500  # with the reason and every other part at their longest, a post still fits in an embed
REPORT_REASON_MAX = 900
REPORTS_PER_HOUR = 20  # from one member, however short the cooldown is
ALSO_MAX = 25  # people named on one report. Past this the post stops being edited and only the count goes up.
ALSO_REASON_SHOWN = 120
HELD_BYTES_MAX = 96 * 1024 * 1024  # reported files in memory at once, across every report in progress
PING_GAP = 60.0  # a flood of reports posts every report and pings staff once in this many seconds
SELF_REFUSAL = "This is about you, so another moderator has to handle it."
THREAD_NAME_MAX = 100
RECONCILE_SECONDS = 90
FULL_PASS_EVERY = 40  # ticks between full passes, so about an hour apart
BULK_DELETE_AGE = timedelta(days=13, hours=23)  # Discord refuses bulk deletes of messages 14 days old or more
PROGRESS_SECONDS = 5.0
PAGE_MAX = 100

LADDER_ACTIONS = ("warn", "mute", "timeout", "kick", "tempban", "ban")
DEFAULT_LADDER = [
    {"action": "warn", "seconds": 0},
    {"action": "mute", "seconds": 24 * 3600},
    {"action": "mute", "seconds": 3 * 86400},
    {"action": "tempban", "seconds": 7 * 86400},
    {"action": "ban", "seconds": 0},
]
EMPTY_COUNTS = {"warn": 0, "mute": 0, "tempban": 0, "ban": 0, "kick": 0, "timeout": 0}
UNDO_ACTIONS = ("unmute", "unminimute", "unban", "untimeout")

DEFAULT_NOTICE = """Hello {user},

We hope this message finds you well. After careful consideration, the moderation team has decided to {action} for violating {rules} of our community guidelines.

{recap}

We kindly request that you read through our community rules again to ensure a clear understanding of the expectations. Please confirm that you have read and understood these rules by replying to this message.

Please respond to this ticket to confirm that you have read the rules and understand the reason for this decision. Note that if we do not receive a reply, {consequence}.

We encourage you to use this time to reflect on your contributions and how they can positively impact the community.

{notes}

Thank you for your understanding and cooperation.

For your convenience, the rules are linked below.
{rules_link}"""

NOTICE_PUNISHMENTS = {
    # value: (action phrase, what happens with no reply)
    "mute": ("apply {article} {duration}mute to your account", "the mute will remain in place until we receive your confirmation"),
    "tempban": ("apply {article} {duration}temporary ban to your account", "the ban will remain in place until we receive your confirmation"),
    "warning": ("issue a formal warning to your account", "this ticket will remain open until we receive your confirmation"),
    "permban": ("apply a permanent ban to your account", "this ticket will remain open until we receive your confirmation"),
    "kick": ("remove you from the server", "this ticket will remain open until we receive your confirmation"),
}
NOTICE_DURATIONS = [("24-hour", "24-hour"), ("3-day", "3-day"), ("7-day", "7-day"), ("14-day", "14-day"), ("30-day", "30-day"), ("Indefinite", "indefinite"), ("Not applicable", "na")]
NOTICE_OPTIONAL = ("recap", "notes", "rules_link")  # a paragraph holding one of these drops out when it's empty

GIF_LINK = re.compile(r"https?://(?:[\w-]+\.)*(?:tenor\.com|giphy\.com)/", re.IGNORECASE)
SAFE_EXTENSION = re.compile(r"[^a-z0-9.]")


# ---- settings checks ----

def ladder_step(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Each step needs an action.")
    action = choice(*LADDER_ACTIONS)(value.get("action"))
    seconds = integer(0, 5 * 365 * 86400)(value.get("seconds") or 0)
    if action in ("tempban", "timeout") and seconds < 60:
        raise Invalid("A temporary ban or timeout needs a length of at least one minute.")
    if action == "timeout" and seconds > 28 * 86400:
        raise Invalid("Discord limits a timeout to 28 days.")
    return {"action": action, "seconds": 0 if action in ("warn", "kick", "ban") else seconds}


def pack_role(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Invalid("Each role needs a name.")
    return {"name": text(95, minimum=1)(value.get("name")), "emoji": text(32)(value.get("emoji") or "")}


# ---- plain functions, kept apart from Discord so they can be tested directly ----

def _many(amount: int, noun: str) -> str:
    return f"{amount} {noun}{'' if amount == 1 else 's'}"


def _fit(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else value[:maximum - 3].rstrip() + "..."


def _code(value: str | None, maximum: int) -> str:
    """Inline code, so what a member wrote can't format the post. Backticks would break out of it, so they go."""
    cleaned = (value or "").replace("`", "")
    return f"`{_fit(cleaned, maximum)}`" if cleaned else "`None`"


def _stamp(moment: float | datetime | None) -> str:
    if moment is None:
        return "`Unknown`"
    seconds = int(moment.timestamp() if isinstance(moment, datetime) else moment)
    return f"<t:{seconds}:F> (<t:{seconds}:R>)"


def _short_stamp(moment: float | datetime | None) -> str:
    if moment is None:
        return "Unknown"
    return f"<t:{int(moment.timestamp() if isinstance(moment, datetime) else moment)}:f>"


def _name(person: Any) -> str:
    return discord.utils.escape_markdown(str(person))


def _message_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def is_thread(channel: Any) -> bool:
    return getattr(channel, "type", None) in (discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread)


def uid_in_name(name: str) -> int | None:
    match = UID_IN_NAME.search(name or "")
    return int(match.group(1)) if match else None


def name_in_thread(name: str) -> str:
    return (name or "").split(" UID ")[0]


def length_label(seconds: int) -> str:
    """How the ladder writes a length: 24-hour, 3-day, 90-minute."""
    if seconds % 86400 == 0 and seconds >= 2 * 86400:
        return f"{seconds // 86400}-day"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}-hour"
    if seconds % 60 == 0:
        return f"{seconds // 60}-minute"
    return f"{seconds}-second"


def length_argument(seconds: int) -> str:
    """The same length as a command argument: 24h, 3d."""
    if seconds % 86400 == 0 and seconds >= 2 * 86400:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(seconds // 60, 1)}m"


def ladder_label(step: dict, mute_default: int = 0) -> str:
    """`mute_default` is how long this server's mute command lasts with no length given, which is what a rung of zero really does."""
    fixed = {"warn": "Warning", "kick": "Kick", "ban": "Permanent ban"}
    if step["action"] in fixed:
        return fixed[step["action"]]
    noun = {"mute": "mute", "timeout": "timeout", "tempban": "temp ban"}[step["action"]]
    if step["seconds"]:
        return f"{length_label(step['seconds'])} {noun}"
    if step["action"] == "mute" and mute_default:
        return f"{length_label(mute_default)} mute, this server's default"
    return f"Indefinite {noun}"


def ladder_command(step: dict, mention: str) -> str:
    action = step["action"]
    who = "user" if action in ("ban", "tempban") else "member"
    length = f" duration:{length_argument(step['seconds'])}" if action in ("mute", "tempban", "timeout") and step["seconds"] else ""
    return f"/{action} {who}:{mention}{length} reason:[reason here]"


def recommended_step(ladder: list[dict], counts: dict) -> int | None:
    """The rung after the highest one this person's record has already reached.

    A rung counts as reached when the record holds at least as many of its action as the ladder has asked for by that point, so with
    the default ladder two mutes reach the second mute rung and the next step is the temp ban.
    """
    if not ladder:
        return None
    asked: dict[str, int] = {}
    reached = -1
    for index, step in enumerate(ladder):
        asked[step["action"]] = asked.get(step["action"], 0) + 1
        if counts.get(step["action"], 0) >= asked[step["action"]]:
            reached = index
    return min(reached + 1, len(ladder) - 1)


def propose_action(ladder: list[dict], counts: dict, mention: str, mute_default: int = 0) -> str:
    if not ladder:
        return "No escalation ladder is set. Add one on the Reports page."
    pick = ladder[recommended_step(ladder, counts)]
    record = f"{_many(counts.get('warn', 0), 'warn')}, {_many(counts.get('mute', 0), 'mute')}, {_many(counts.get('tempban', 0), 'temp ban')}"
    ending = ["", f"**Recommended Next Action:** {ladder_label(pick, mute_default)}. Record so far: {record}.", f"**Command:** `{ladder_command(pick, mention)}`"]

    def path(with_commands: bool) -> list[str]:
        lines = ["**Escalation Path:**"]
        for index, step in enumerate(ladder):
            lead = "First offense" if index == 0 else "Next"
            lines.append(f"- {lead}: {ladder_label(step, mute_default)}" + (f" `{ladder_command(step, mention)}`" if with_commands else ""))
        return lines

    # A long ladder's commands won't all fit a field, and the recommendation is what has to survive.
    full = "\n".join(path(True) + ending)
    return full if len(full) <= 1024 else _fit("\n".join(path(False) + ending), 1024)


def format_rules_phrase(raw: str) -> str:
    """'3' gives 'rule 3', and '1, 3, 5' gives 'rules 1, 3, and 5'."""
    numbers = re.findall(r"\d+", raw)
    if not numbers:
        return f"rule {raw.strip()}"
    if len(numbers) == 1:
        return f"rule {numbers[0]}"
    if len(numbers) == 2:
        return f"rules {numbers[0]} and {numbers[1]}"
    return "rules " + ", ".join(numbers[:-1]) + f", and {numbers[-1]}"


def format_recap(raw: str) -> str:
    """Each line 'N: does something' becomes 'Rule N does something', joined into one sentence. Other lines pass through as they are."""
    parts = []
    for line in raw.splitlines():
        line = line.strip().rstrip(".")
        if not line:
            continue
        match = re.match(r"^(\d+)\s*[:\-]\s*(.+)$", line)
        parts.append(f"Rule {match.group(1)} {match.group(2)}" if match else line)
    if not parts:
        return ""
    if len(parts) == 1:
        return f"To recap, {parts[0]}."
    return "To recap, " + ", ".join(parts[:-1]) + f", and {parts[-1]}."


def build_notice(template: str, *, mention: str, punishment: str, duration: str, rules: str, recap: str, notes: str, rules_link: str) -> str:
    action, consequence = NOTICE_PUNISHMENTS[punishment]
    length, article = "", "a"
    if punishment in ("mute", "tempban") and duration and duration != "na":
        length = f"{duration} "
        article = "an" if duration[0].lower() in "aeiou" else "a"
    values = {
        "user": mention,
        "action": action.format(article=article, duration=length),
        "rules": format_rules_phrase(rules),
        "recap": format_recap(recap),
        "consequence": consequence,
        "notes": (notes or "").strip(),
        "rules_link": (rules_link or "").strip(),
    }
    paragraphs = []
    for paragraph in template.split("\n\n"):
        if any(f"{{{name}}}" in paragraph and not values[name] for name in NOTICE_OPTIONAL):
            continue
        for name, value in values.items():  # plain replacing, so a stray brace in the template can't raise
            paragraph = paragraph.replace(f"{{{name}}}", value)
        paragraphs.append(paragraph)
    return "\n\n".join(paragraphs).strip()


def in_pieces(parts: list[str], *, joiner: str = "\n", maximum: int = 2000) -> list[str]:
    """Join text into pieces Discord will take, breaking between parts wherever it can."""
    pieces, current = [], ""
    for part in parts:
        while len(part) > maximum:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(part[:maximum])
            part = part[maximum:]
        joined = f"{current}{joiner}{part}" if current else part
        if len(joined) > maximum:
            pieces.append(current)
            current = part
        else:
            current = joined
    if current:
        pieces.append(current)
    return pieces


def parse_user_id(raw: str) -> int | None:
    """A mention or a plain ID. Works for people who have left, since it never looks them up."""
    cleaned = raw.strip()
    if cleaned.startswith("<@") and cleaned.endswith(">"):
        cleaned = cleaned[2:-1].lstrip("!")
    return whole_number(cleaned)


def message_texts(message: Any) -> list[str]:
    """Everything a keyword is checked against: the text and every embed's description and fields."""
    texts = [message.content or ""]
    for embed in message.embeds:
        if embed.description:
            texts.append(embed.description)
        for embed_field in embed.fields or []:
            texts.extend((embed_field.name or "", embed_field.value or ""))
    return texts


def has_gif(message: Any) -> bool:
    for attachment in message.attachments:
        if (attachment.content_type or "").lower() == "image/gif" or (attachment.filename or "").lower().endswith(".gif"):
            return True
    if GIF_LINK.search(message.content or ""):
        return True
    return any(getattr(embed, "type", None) == "gifv" for embed in message.embeds)


def rounded_duration(seconds: int) -> str:
    if not seconds:
        return "indefinite"
    if seconds < 3600:
        return f"{seconds // 60} minutes"
    if seconds < 86400:
        return f"{(seconds + 3599) // 3600} hours"
    return f"{(seconds + 86399) // 86400} days"


def pack_entries(raw: list[dict], *, prefix: bool) -> list[tuple[str, str, str]]:
    """(base name, emoji, the name the role gets), alphabetical, ignoring case."""
    out = []
    for row in sorted(raw, key=lambda r: r["name"].casefold()):
        emoji = row.get("emoji") or ""
        out.append((row["name"], emoji, f"{emoji} {row['name']}" if prefix and emoji else row["name"]))
    return out


def find_pack_role(roles: Any, base_name: str, emoji: str = "") -> Any:
    """A pack role whether it was created bare or with its emoji in front. Only the exact name counts: a pack entry "Mod" must never find "Head Mod", because deploying moves what it finds and deleting deletes it."""
    names = {base_name, f"{emoji} {base_name}"} if emoji else {base_name}
    return discord.utils.find(lambda r: r.name in names, roles)


# ---- what a report carries ----

@dataclass
class Evidence:
    """One file from the reported message, held in memory while the report goes out, so each post can upload its own copy."""
    name: str
    original: str
    content_type: str | None
    size: int
    url: str
    spoiler: bool = False
    data: bytes | None = None
    held: int = 0  # room taken in the EvidenceHold, given back when the report has gone out

    @property
    def is_image(self) -> bool:
        return (self.content_type or "").startswith("image/")

    def file(self) -> discord.File:
        return discord.File(io.BytesIO(self.data or b""), filename=self.name, spoiler=self.spoiler)


@dataclass
class Draft:
    guild: Any
    kind: str
    reporter: Any
    target: Any
    reason: str
    channel_id: int | None = None
    message_id: int | None = None
    message_created_at: float | None = None
    content: str = ""
    attachment_count: int = 0
    reaction_count: int = 0
    jump_url: str | None = None
    filed_in: int | None = None
    evidence: list[Evidence] = field(default_factory=list)
    from_webhook: bool = False
    attachments: list = field(default_factory=list)  # read only once it's known this report will really post
    file_limit: int = 0
    known: bool = True  # someone never seen in this server gets a post but no ping and no thread, since /report takes any ID
    earlier: tuple[int, int] = (0, 0)  # earlier reports on this person, and how many of those are open
    in_voice: list[str] = field(default_factory=list)  # a voice report's names of everyone in the channel when it was filed


class _NoOneNamed:
    """What stands in for the target of a report that names no one. The table holds 0 as the ID, which no account can have."""
    id = 0
    mention = "no one"
    name = "No one named"
    display_name = "No one named"
    global_name = None
    bot = False
    joined_at = None
    created_at = None

    def __str__(self) -> str:
        return self.name


NO_ONE_NAMED = _NoOneNamed()
PLACE = "place"  # a report's kind when it names no one and points at a channel
VOICE = "voice"  # a report on someone in voice, from /vc_report or the VC Report menu. Its channel_id is the voice channel they were in.
VOICE_NAMES_MAX = 1000  # who else was in that voice channel. A voice report has no message content, so this fits where content would.


class ReportFailed(Exception):
    """The report couldn't be posted. The text is for the person filing it."""


class ThreadLookupFailed(Exception):
    """Discord didn't answer about a thread we know of, which says nothing about whether it still exists."""


class EvidenceHold:
    """Reported files wait in memory until their posts are out, and those can wait a long time behind a member's lock. This keeps a flood from growing that without limit. A file past the limit is named as not copied."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.held = 0

    def take(self, size: int) -> bool:
        if self.held + size > self.limit:
            return False
        self.held += size
        return True

    def give_back(self, size: int) -> None:
        self.held = max(self.held - size, 0)

    def release(self, evidence: list[Evidence]) -> None:
        """Lets go of these files. A second call gives nothing back."""
        for item in evidence:
            self.give_back(item.held)
            item.held = 0
            item.data = None


async def copy_attachments(attachments: list, limit: int, hold: EvidenceHold | None = None) -> list[Evidence]:
    """Read the files now. A deleted message's files stop being served, and these copies are what staff keep."""
    copies: list[Evidence] = []
    total = 0
    for index, attachment in enumerate(attachments, 1):
        filename = attachment.filename or "file"
        _, extension = os.path.splitext(filename)
        # Discord matches attachment:// links against plain names only, so the copy gets one and the post lists the original.
        safe = SAFE_EXTENSION.sub("", extension.lower())[:10]
        item = Evidence(
            name=f"evidence-{index}{safe}", original=filename, content_type=attachment.content_type, size=attachment.size,
            url=attachment.url, spoiler=attachment.is_spoiler(),
        )
        copies.append(item)
        if index > FILES_PER_MESSAGE or attachment.size > limit or total + attachment.size > TOTAL_UPLOAD_BYTES:
            continue
        if hold is not None:
            if not hold.take(attachment.size):
                continue
            item.held = attachment.size
        try:
            item.data = await attachment.read()
            total += len(item.data)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            log.warning("Could not copy attachment %s: %s", filename, error)
            if hold is not None:
                hold.give_back(item.held)
                item.held = 0
    return copies


# ---- what a report says ----

def report_text(draft: Draft) -> tuple[str, str | None]:
    """The post's text, and the whole thing as a file when the text had to be cut to fit."""
    reporter, target = draft.reporter, draft.target
    joined = getattr(target, "joined_at", None)
    if draft.kind == PLACE:
        parts = [f"{reporter.mention} ({_name(reporter)}) filed a report that names no one."]
        if draft.filed_in:
            parts.append(f"**Filed in**\n<#{draft.filed_in}>")
        parts.append(f"**Reason**\n{_code(draft.reason, REPORT_REASON_MAX)}")
        copied = [e for e in draft.evidence if e.data is not None]
        if copied:
            parts.append("**Files kept with this report**\n" + _fit("\n".join(f"`{e.name}`: {_code(e.original, 80)}" for e in copied), 400))
        whole = f"{reporter} filed a report that names no one.\n\nReason:\n{draft.reason}\n" if len(draft.reason) > REPORT_REASON_MAX else None
        return "\n\n".join(parts), whole
    if draft.kind == "message":
        lead = f"{reporter.mention} ({_name(reporter)}) reported [this message]({draft.jump_url}) from {target.mention} ({_name(target)})."
    elif draft.kind == VOICE:
        lead = f"{reporter.mention} ({_name(reporter)}) reported {target.mention} ({_name(target)}) in voice."
    else:
        lead = f"{reporter.mention} ({_name(reporter)}) reported {target.mention} ({_name(target)})."
    parts = [
        lead,
        "**Reported member**\n"
        f"Name: {_code(str(target), 100)}\n"
        f"ID: `{target.id}`\n"
        f"Account created: {_stamp(target.created_at)}\n"
        f"Joined server: {_stamp(joined)}" + (f"\nEarlier reports: `{draft.earlier[0]}` (`{draft.earlier[1]}` open)" if draft.earlier[0] else ""),
    ]
    if draft.kind == "message":
        parts.append(
            "**Reported message**\n"
            f"Message ID: `{draft.message_id}`\n"
            f"Channel: <#{draft.channel_id}>\n"
            f"Created: {_stamp(draft.message_created_at)}\n"
            f"Attachments: `{draft.attachment_count}`\n"
            f"Reactions: `{draft.reaction_count}`\n"
            f"Content: {_code(draft.content, REPORT_CONTENT_MAX)}"
        )
    else:
        if draft.kind == VOICE:
            parts.append(voice_text(draft))
        if draft.filed_in:
            parts.append(f"**Filed in**\n<#{draft.filed_in}>")
    parts.append(f"**Reason**\n{_code(draft.reason, REPORT_REASON_MAX)}")
    copied = [e for e in draft.evidence if e.data is not None]
    missed = [e for e in draft.evidence if e.data is None]
    if copied:
        parts.append("**Files kept with this report**\n" + _fit("\n".join(f"`{e.name}`: {_code(e.original, 80)}" for e in copied), 400))
    if missed:
        parts.append("**Files not copied**\n" + _fit("\n".join(_code(e.original, 80) for e in missed), 300))
    body = "\n\n".join(parts)
    cut = len(draft.content) > REPORT_CONTENT_MAX or len(draft.reason) > REPORT_REASON_MAX
    if not cut:
        return body, None
    if draft.kind == VOICE:
        return body, f"{reporter} reported {target} ({target.id}) in voice\n\nVoice channel ID: {draft.channel_id or 'none'}\n\nReason:\n{draft.reason}\n"
    return body, (
        f"{reporter} reported {target} ({target.id})\n\nMessage ID: {draft.message_id}\nChannel ID: {draft.channel_id}\n\n"
        f"Content:\n{draft.content}\n\nReason:\n{draft.reason}\n"
    )


def voice_text(draft: Draft) -> str:
    """Where a voice report's target was and who was there with them. Both are gone a minute later, so the post keeps them."""
    if not draft.channel_id:
        return "**Voice channel**\nNot in voice when reported"
    text = f"**Voice channel**\n<#{draft.channel_id}>"
    if draft.in_voice:
        text += "\nIn the channel: " + _fit(", ".join(draft.in_voice), VOICE_NAMES_MAX)
    return text


def state_line(row: dict) -> str | None:
    who = f"<@{row['handled_by']}> ({discord.utils.escape_markdown(row['handled_name'] or 'unknown')})"
    if row["state"] == ACTIONED:
        return f"\N{WHITE HEAVY CHECK MARK} Handled by {who}."
    if row["state"] == FALSE_POSITIVE:
        return f"\N{CROSS MARK} Marked as a false report by {who}."
    if row.get("reset_by"):
        return f"Reopened by <@{row['reset_by']}> ({discord.utils.escape_markdown(row['reset_name'] or 'unknown')})."
    return None


def also_reported_text(row: dict, room: int) -> str:
    """Everyone else who reported this, as many as `room` holds. The dashboard has them all."""
    also = json.loads(row.get("also_reported") or "[]")
    count = max(row.get("also_count") or 0, len(also))
    if not count:
        return ""
    lines = ["**Also reported by**"]
    used = len(lines[0])
    shown = 0
    for person in also:
        line = f"<@{person['id']}> ({discord.utils.escape_markdown(person['name'])}): {_code(person['reason'], ALSO_REASON_SHOWN)}"
        if used + len(line) + 40 > room:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < count:
        lines.append(f"And {count - shown} more, on the dashboard.")
    return "\n".join(lines) if len(lines) > 1 else ""


def full_report_text(row: dict) -> str:
    tail = []
    line = state_line(row)
    if line:
        tail.append(line)
    if row.get("case_number"):
        tail.append(f"Linked case: `{row['case_number']}`")
    head = row["post_text"] or ""
    # Who handled it goes last and has to survive, so the names only get the room that's left.
    also = also_reported_text(row, 4096 - len(head) - sum(len(part) + 2 for part in tail) - 4)
    return "\n\n".join(p for p in [head, also, *tail] if p)


def report_title(row: dict) -> str:
    return {"message": "Message Report", PLACE: "Report", VOICE: "Voice Report"}.get(row["kind"], "Member Report")


def jump_label(row: dict) -> str:
    return "Jump to message" if row["kind"] == "message" else "Jump to where it was filed"


def report_controls(row: dict) -> list[dict]:
    """A report post's buttons. They stay usable after a restart because ReportButtons answers them by ID."""
    handled = row["state"] != OPEN
    rows = []
    if row.get("jump_url"):
        rows.append({"type": "link", "url": row["jump_url"], "label": jump_label(row)})
    rows.append({"type": "button", "custom_id": REPORT_ACTIONED_ID, "label": "Actioned", "style": "success", "disabled": handled})
    rows.append({"type": "button", "custom_id": REPORT_FALSE_ID, "label": "False Positive", "style": "danger", "disabled": handled})
    if row["kind"] != PLACE:  # with no one named, there's no one's history to view
        rows.append({"type": "button", "custom_id": REPORT_HISTORY_ID, "label": "View Message History", "style": "secondary"})
    return rows


def checklist_controls(step: int, *, can_acknowledge: bool = False) -> list[dict]:
    """Only the first step's button and an open report's acknowledge button do anything. The others name the step in hand."""
    enabled = step == 0 or (step == 3 and can_acknowledge)
    return [
        {"type": "button", "custom_id": REVIEW_START_ID, "label": STEP_BUTTONS[min(step, len(STEP_BUTTONS) - 1)], "style": "primary", "disabled": not enabled},
        {"type": "button", "custom_id": REVIEW_DONE_ID, "label": "Mark Step Completed", "style": "success"},
        {"type": "button", "custom_id": REVIEW_DISMISS_ID, "label": "Dismiss", "style": "secondary"},
    ]


def controls(rows: list[dict]) -> discord.ui.View:
    return component_view({}, rows)


# ---- buttons and forms ----

class ReportButtons(discord.ui.View):
    """Answers the buttons on every report post, across restarts. It holds no state: the row found by the message's ID does."""

    def __init__(self, cog: "Reports") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        await self.cog.tell_button_failed(interaction, error, item)

    @discord.ui.button(label="Actioned", style=discord.ButtonStyle.success, custom_id=REPORT_ACTIONED_ID)
    async def actioned(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_state(interaction, ACTIONED)

    @discord.ui.button(label="False Positive", style=discord.ButtonStyle.danger, custom_id=REPORT_FALSE_ID)
    async def false_positive(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_state(interaction, FALSE_POSITIVE)

    @discord.ui.button(label="View Message History", style=discord.ButtonStyle.secondary, custom_id=REPORT_HISTORY_ID)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_history(interaction)


class ReviewButtons(discord.ui.View):
    """Answers the buttons on every checklist, across restarts. Which step a click means comes from the database."""

    def __init__(self, cog: "Reports") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        await self.cog.tell_button_failed(interaction, error, item)

    @discord.ui.button(label="Gather Recent Messages & Logs", style=discord.ButtonStyle.primary, custom_id=REVIEW_START_ID)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_start(interaction)

    @discord.ui.button(label="Mark Step Completed", style=discord.ButtonStyle.success, custom_id=REVIEW_DONE_ID)
    async def completed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_completed(interaction)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, custom_id=REVIEW_DISMISS_ID)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.press_dismiss(interaction)


class Form(discord.ui.Modal):
    """A form whose failure is answered instead of leaving the person with "something went wrong" from Discord."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Form %s failed", self.title, exc_info=error)
        message = "Something went wrong with that form. It's been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


class ReasonForm(Form):
    def __init__(self, title: str, submit: Any) -> None:
        super().__init__(title=title)
        self.submit = submit
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, placeholder="What happened?", required=True, max_length=2000)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.submit(interaction, self.reason.value)


class GatherForm(Form):
    def __init__(self, cog: "Reports") -> None:
        super().__init__(title="Specify Message Counts")
        self.cog = cog
        self.message_count = discord.ui.TextInput(label="Number of User Messages", placeholder="How many of their messages to post here", default="25", required=True, max_length=4)
        self.log_count = discord.ui.TextInput(label="Number of Message Logs", placeholder="How many of their log entries to post here", default="25", required=True, max_length=4)
        self.add_item(self.message_count)
        self.add_item(self.log_count)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_gather(interaction, self.message_count.value, self.log_count.value)


class NoticeForm(Form):
    def __init__(self, cog: "Reports", target: Any) -> None:
        super().__init__(title="Punishment notice")
        self.cog = cog
        self.target = target
        self.punishment = discord.ui.Select(options=[
            discord.SelectOption(label="Mute", value="mute", default=True),
            discord.SelectOption(label="Temporary ban", value="tempban"),
            discord.SelectOption(label="Warning", value="warning"),
            discord.SelectOption(label="Permanent ban", value="permban"),
            discord.SelectOption(label="Kick", value="kick"),
        ])
        self.duration = discord.ui.Select(options=[discord.SelectOption(label=label, value=value, default=value == "24-hour") for label, value in NOTICE_DURATIONS])
        self.rules = discord.ui.TextInput(placeholder="For example: 1, 3", max_length=50)
        self.recap = discord.ui.TextInput(style=discord.TextStyle.paragraph, placeholder="3: prohibits spam and self-promotion\n5: enforces respectful conduct", max_length=500)
        self.notes = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=200)
        self.add_item(discord.ui.Label(text="Punishment", component=self.punishment))
        self.add_item(discord.ui.Label(text="Duration", description="Ignored for punishments without a duration", component=self.duration))
        self.add_item(discord.ui.Label(text="Rule numbers", component=self.rules))
        self.add_item(discord.ui.Label(text="Rule recap", description='One rule per line as "number: verb phrase"', component=self.recap))
        self.add_item(discord.ui.Label(text="Additional notes", description="Optional paragraph inserted before the closing", component=self.notes))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = self.cog.cfg(interaction.guild_id)
        content = build_notice(
            cfg["notice_template"], mention=self.target.mention,
            punishment=(self.punishment.values or ["mute"])[0], duration=(self.duration.values or ["24-hour"])[0],
            rules=self.rules.value, recap=self.recap.value, notes=self.notes.value, rules_link=cfg["notice_rules_link"],
        )
        preview = discord.Embed(description=_fit(content, 4096), color=0x00A2F8)
        await interaction.response.send_message(
            "**Preview.** Nothing is posted until you press Send.", embed=preview,
            view=NoticeSendView(content, self.target), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )


class NoticeSendView(discord.ui.View):
    """Preview controls. Not persistent on purpose: an unsent draft should not survive a restart."""

    def __init__(self, content: str, target: Any) -> None:
        super().__init__(timeout=600)
        self.content = content
        self.target = target

    @discord.ui.button(label="Send to this channel", style=discord.ButtonStyle.success)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            for piece in in_pieces(self.content.split("\n\n"), joiner="\n\n"):
                await interaction.channel.send(piece, allowed_mentions=discord.AllowedMentions(users=[self.target], everyone=False, roles=False))
        except discord.Forbidden:
            await interaction.response.edit_message(content="I can't post in this channel.", embed=None, view=None)
            return
        except discord.HTTPException as error:
            await interaction.response.edit_message(content=f"Discord refused the notice: {error.text or error.status}", embed=None, view=None, allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.edit_message(content="Sent.", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Canceled. Nothing was posted.", embed=None, view=None)
        self.stop()


class OpenNoticeView(discord.ui.View):
    """A text command can't open a form. Only a button or a slash command can, so a text command gets this button."""

    def __init__(self, cog: "Reports", author_id: int, target: Any) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.target = target

    @discord.ui.button(label="Open the form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran this command can use this.", ephemeral=True)
            return
        await interaction.response.send_modal(NoticeForm(self.cog, self.target))
        self.stop()
        if interaction.message is not None:  # it has done its job, and nothing should stay behind in the channel
            await _delete_quietly(interaction.message)


class DeleteFlags(commands.FlagConverter, case_insensitive=True):
    channel: discord.TextChannel | discord.Thread | None = commands.flag(default=None, description="The channel or thread to delete in. Defaults to this one.")
    user: str | None = commands.flag(default=None, description="A mention or ID. Works for people who have left.")
    count: int | None = commands.flag(default=None, description="How many recent messages to look through")
    until_date: str | None = commands.flag(default=None, description="Look back no further than this date, as YYYY-MM-DD")
    keyword: str | None = commands.flag(default=None, description="A word or phrase, or a regex starting with re:")
    gifs_only: bool = commands.flag(default=False, description="Only messages with a GIF")


async def _delete_quietly(message: Any) -> bool:
    """Whether the message is gone."""
    if message is None:
        return True
    try:
        await message.delete()
    except discord.NotFound:
        return True
    except discord.HTTPException:
        return False
    return True


# ---- the feature ----

class Reports(Module):
    key = "reports"
    category = "Moderation"
    child_links = {"review_state": {"report_id": "reports.id"}}
    schema = Schema({
        "report_channel": Field(None, optional(snowflake)),
        "alert_role": Field(None, optional(snowflake)),
        "banned_role": Field(None, optional(snowflake)),
        "report_self": Field(True, boolean),
        "report_bots": Field(True, boolean),
        "report_admins": Field(True, boolean),
        "cooldown_seconds": Field(10, integer(0, 3600)),
        "forum_channel": Field(None, optional(snowflake)),
        "moderator_role": Field(None, optional(snowflake)),
        "auto_review": Field(True, boolean),
        "ticket_command": Field("/new user:{user}", text(200)),
        "mod_ping_text": Field("@moderator", text(50)),
        "log_channel_fallback": Field(None, optional(snowflake)),
        "ladder": Field(DEFAULT_LADDER, list_of(ladder_step, 12, unique=False)),
        "notice_template": Field(DEFAULT_NOTICE, text(4000, minimum=1)),
        "notice_rules_link": Field("", text(300)),
        "fun_roles": Field([], list_of(pack_role, 100)),
    })
    migrations = [
        """
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',
            reporter_id INTEGER NOT NULL,
            reporter_name TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            target_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            message_created_at REAL,
            content TEXT,
            attachment_count INTEGER NOT NULL DEFAULT 0,
            reaction_count INTEGER NOT NULL DEFAULT 0,
            files TEXT NOT NULL DEFAULT '[]',
            jump_url TEXT,
            post_channel_id INTEGER,
            post_message_id INTEGER,
            post_text TEXT,
            thread_id INTEGER,
            handled_by INTEGER,
            handled_name TEXT,
            handled_at REAL,
            reset_by INTEGER,
            reset_name TEXT,
            case_number INTEGER,
            created_at REAL NOT NULL,
            UNIQUE (guild_id, number)
        );
        CREATE INDEX reports_recent ON reports (guild_id, id DESC);
        CREATE INDEX reports_by_state ON reports (guild_id, state, id DESC);
        CREATE INDEX reports_by_target ON reports (guild_id, target_id, id DESC);
        CREATE INDEX reports_by_reporter ON reports (guild_id, reporter_id, id DESC);
        CREATE INDEX reports_by_post ON reports (post_message_id);
        CREATE TABLE review_threads (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            thread_id INTEGER NOT NULL,
            stats_message_id INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE INDEX review_threads_by_thread ON review_threads (thread_id);
        CREATE TABLE review_state (
            thread_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            current_step INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            modal_message_id INTEGER,
            report_id INTEGER,
            updated_at REAL NOT NULL
        );
        CREATE INDEX review_state_by_status ON review_state (status, thread_id);
        CREATE INDEX review_state_by_guild ON review_state (guild_id, updated_at DESC);
        CREATE TABLE moderator_pings (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_at REAL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE INDEX moderator_pings_top ON moderator_pings (guild_id, count DESC);
        """,
        # The others who reported the same thing while the report was open, as a JSON list that stops growing at ALSO_MAX. The count keeps going.
        """
        ALTER TABLE reports ADD COLUMN also_reported TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE reports ADD COLUMN also_count INTEGER NOT NULL DEFAULT 0;
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._filed: dict[tuple[int, int], list[float]] = {}  # when each member last filed, for the limit per hour
        self._pinged: dict[tuple[str, int], float] = {}  # when each server's staff were last pinged about a report
        self._held = EvidenceHold(HELD_BYTES_MAX)
        self._locks: dict[Any, asyncio.Lock] = {}
        self._gathering: set[int] = set()  # threads with a gather running, which the reconcile loop must leave alone
        self._flagged: set[int] = set()  # threads whose checklist message was seen being deleted
        self._forums: dict[int, int | None] = {}
        self._started = False  # a gateway reconnect fires on_ready again, and the startup work must run once
        self._ticks = 0
        self._background: set[asyncio.Future] = set()
        self.regex = RegexGuard()  # patterns typed into the delete command run in a process that can be killed
        self._menus = [
            app_commands.ContextMenu(name="Report Message", callback=self.menu_report_message),
            app_commands.ContextMenu(name="Report User", callback=self.menu_report_user),
            app_commands.ContextMenu(name="Reset Report", callback=self.menu_reset_report),
        ]

    async def on_load(self) -> None:
        self.bot.add_view(ReportButtons(self))
        self.bot.add_view(ReviewButtons(self))
        for menu in self._menus:
            self.bot.tree.add_command(menu)
        self.bot.settings.listen(self.key, self._settings_saved)

    async def cog_unload(self) -> None:
        self._reconcile_loop.cancel()
        for task in list(self._background):
            task.cancel()
        await self.regex.close()
        for menu in self._menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    # ---- small shared pieces ----

    def _spawn(self, work: Any) -> None:
        async def guarded() -> None:
            try:
                await work
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Background work in reports failed")
        task = asyncio.ensure_future(guarded())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _lock(self, key: Any) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) > 2000:
                for old in [k for k, held in self._locks.items() if not held.locked()]:
                    del self._locks[old]
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def _thread_lock(self, thread_id: int) -> asyncio.Lock:
        """One per thread. Two moderators clicking at the same moment would otherwise each post a checklist before either recorded its ID."""
        return self._lock(("thread", thread_id))

    def staff_roles(self, settings: dict) -> dict[str, set[int]]:
        return {"moderator_role": {settings["moderator_role"]} if settings["moderator_role"] else set()}

    def power_roles(self, settings: dict) -> dict[str, set[int]]:
        return {"alert_role": {settings["alert_role"]} if settings["alert_role"] else set()}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        return {name: {settings[name]} if settings[name] else set() for name in ("report_channel", "forum_channel", "log_channel_fallback")}

    def pinged_channels(self, settings: dict) -> dict[str, set[int]]:
        channel, role_id = settings["report_channel"], settings["alert_role"]
        if channel and role_id and delivery.needs_ping_right(getattr(self.bot.get_channel(channel), "guild", None), role_id):
            return {f"alert_role:{role_id}": {channel}}
        return {}

    def is_staff(self, member: Any) -> bool:
        """Who may work reports and reviews: Discord's moderation permissions, or a role the server named."""
        permissions = getattr(member, "guild_permissions", None)
        if permissions is None:
            return False
        if permissions.administrator or permissions.manage_guild or permissions.moderate_members or permissions.kick_members or permissions.ban_members or permissions.manage_messages:
            return True
        guild_id = member.guild.id
        cfg = self.cfg(guild_id)
        named = {cfg["moderator_role"], self.bot.settings.get(guild_id, COMMANDS_MODULE).get("mod_role")}
        named |= set(self.bot.settings.get(guild_id, "core").get("dashboard_staff_roles") or [])
        named.discard(None)
        return bool(named & {role.id for role in member.roles})

    async def tell_button_failed(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        log.error("Button %s failed", getattr(item, "custom_id", item), exc_info=error)
        message = "Something went wrong with that button. It's been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _member_record(self, guild_id: int, user_id: int) -> dict:
        """A member's cases and counts, from the moderation module. A server without it has an empty record."""
        history = getattr(self.bot.module("moderation"), "history", None)
        if history is None:
            return {"cases": [], "notes": [], "counts": dict(EMPTY_COUNTS)}
        record = await history(guild_id, user_id)
        return {**record, "counts": {**EMPTY_COUNTS, **(record.get("counts") or {})}}

    def _report_channel(self, guild: Any) -> Any:
        channel_id = self.cfg(guild.id)["report_channel"]
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        return channel if channel is not None and hasattr(channel, "send") else None

    def _forum(self, guild: Any) -> Any:
        channel_id = self.cfg(guild.id)["forum_channel"]
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if channel is not None and channel.type == discord.ChannelType.forum else None

    # ---- filing a report ----

    def _refusal(self, guild: Any, reporter: Any, target: Any, *, noun: str) -> str | None:
        """Why this person can't report this target, in a sentence for them. `noun` is what they're reporting."""
        cfg = self.cfg(guild.id)
        if target.id == guild.me.id:
            return f"You can't report this bot's own {noun}."
        if self._report_channel(guild) is None:
            return "Reports aren't set up on this server yet. Ask an admin to choose a reports channel on the dashboard."
        banned = cfg["banned_role"]
        if banned and any(role.id == banned for role in reporter.roles):
            return "You have a role that stops you from filing reports."
        if not cfg["report_self"] and target.id == reporter.id:
            return "You can't report yourself on this server."
        if not cfg["report_bots"] and target.bot:
            return "You can't report a bot on this server."
        permissions = getattr(target, "guild_permissions", None)
        if not cfg["report_admins"] and permissions is not None and permissions.administrator and not target.bot and target.id != reporter.id:
            return "You can't report a server administrator on this server."
        return None

    def _cooldown_left(self, member: Any) -> float:
        if member.guild_permissions.manage_guild:
            return 0.0
        key = (member.guild.id, member.id)
        now = time.monotonic()
        left = self._cooldowns.get(key, 0.0) - now
        recent = [stamp for stamp in self._filed.get(key, ()) if now - stamp < 3600]
        if len(recent) >= REPORTS_PER_HOUR:
            left = max(left, 3600 - (now - recent[0]))
        return max(0.0, left)

    def _start_cooldown(self, member: Any) -> None:
        now = time.monotonic()
        if len(self._cooldowns) > 5000:
            self._cooldowns = {key: ready for key, ready in self._cooldowns.items() if ready > now}
        if len(self._filed) > 5000:
            self._filed = {key: stamps for key, stamps in self._filed.items() if now - stamps[-1] < 3600}
        key = (member.guild.id, member.id)
        self._cooldowns[key] = now + self.cfg(member.guild.id)["cooldown_seconds"]
        self._filed[key] = [stamp for stamp in self._filed.get(key, ()) if now - stamp < 3600][-(REPORTS_PER_HOUR - 1):] + [now]

    @staticmethod
    def _wait_text(left: float) -> str:
        if left >= 120:
            return f"Wait {math.ceil(left / 60)} minutes before filing another report."
        return f"Wait {left:.1f} seconds before filing another report."

    def _ping_free(self, kind: str, guild_id: int) -> bool:
        """Whether a ping can go out now, and if so it counts as sent. Staff pinged a moment ago are already looking, and the post is there either way."""
        now = time.monotonic()
        if now - self._pinged.get((kind, guild_id), -PING_GAP) < PING_GAP:
            return False
        self._pinged[(kind, guild_id)] = now
        return True

    def _subject_refusal(self, member: Any, subject_id: int) -> str | None:
        return SELF_REFUSAL if member.id == subject_id and not access.is_admin(member) else None

    async def _known_here(self, guild: Any, user: Any) -> bool:
        """Whether this person has ever been part of this server, as far as anything here can say. /report takes any Discord ID, and a name no one has seen shouldn't cost a thread and a ping."""
        if isinstance(user, discord.Member) or guild.get_member(user.id) is not None:
            return True
        for table in ("review_threads", "reports"):
            column = "user_id" if table == "review_threads" else "target_id"
            if await self.db.scalar(f"SELECT 1 FROM {table} WHERE guild_id = ? AND {column} = ? LIMIT 1", (guild.id, user.id)):
                return True
        record = await self._member_record(guild.id, user.id)
        if record["cases"]:
            return True
        stored = getattr(self.bot.module("logging"), "user_messages", None)
        return bool(stored and await stored(guild.id, user.id, limit=1))

    async def _next_number(self, guild_id: int) -> int:
        return await self.db.scalar("SELECT COALESCE(MAX(number), 0) + 1 FROM reports WHERE guild_id = ?", (guild_id,))

    async def file_report(self, draft: Draft) -> dict:
        """Store the report and post it. Raises ReportFailed when no post could go out, and leaves no row behind then."""
        guild = draft.guild
        channel = self._report_channel(guild)
        if channel is None:
            raise ReportFailed("The reports channel no longer exists. Tell an admin.")
        if draft.kind == "user":
            draft.known = await self._known_here(guild, draft.target)
        counted = await self.db.one("SELECT COUNT(*) AS amount, COALESCE(SUM(state = 'open'), 0) AS still_open FROM reports WHERE guild_id = ? AND target_id = ?", (guild.id, draft.target.id))
        draft.earlier = (counted["amount"], counted["still_open"])
        body, whole = report_text(draft)
        kept = self._kept_files(draft)
        now = time.time()
        async with self.db.transaction():
            number = await self._next_number(guild.id)
            report_id = await self.db.insert(
                "INSERT INTO reports (guild_id, number, kind, reporter_id, reporter_name, target_id, target_name, reason, channel_id, message_id, "
                "message_created_at, content, attachment_count, reaction_count, files, jump_url, post_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild.id, number, draft.kind, draft.reporter.id, str(draft.reporter), draft.target.id, str(draft.target), draft.reason, draft.channel_id,
                 draft.message_id, draft.message_created_at, draft.content, draft.attachment_count, draft.reaction_count, json.dumps(kept), draft.jump_url, body, now),
            )
        row = await self.report_by_id(guild.id, report_id)
        try:
            sent = await self._send_report_post(channel, row, draft, whole)
        except Exception as error:
            await self.db.run("DELETE FROM reports WHERE id = ?", (report_id,))
            if not isinstance(error, discord.HTTPException):
                raise
            log.warning("Could not post report in %s: %s", channel.id, error)
            if error.status >= 500:  # Discord itself failed, and the same report will go through in a moment
                raise ReportFailed("Discord didn't take the report just now. Try again.") from error
            raise ReportFailed("The report couldn't reach the reports channel. Tell an admin.") from error
        await self.db.run("UPDATE reports SET post_channel_id = ?, post_message_id = ? WHERE id = ?", (channel.id, sent.id, report_id))
        row.update(post_channel_id=channel.id, post_message_id=sent.id)
        return row

    @staticmethod
    def _kept_files(draft: Draft) -> list[dict]:
        kept = []
        for item in draft.evidence:
            entry = {"filename": item.original, "name": item.name, "size": item.size, "content_type": item.content_type, "copied": item.data is not None}
            if item.data is None:
                entry["url"] = item.url
            kept.append(entry)
        return kept

    async def _open_duplicate(self, draft: Draft) -> dict | None:
        """An open report this one can join: the same message, or the same person with nothing new to show. One whose post is gone can't take names."""
        if draft.kind == VOICE:
            return None  # each one shows who was in the channel at that moment, which a name added to another report would lose
        if draft.kind == "message":
            row = await self.db.one("SELECT * FROM reports WHERE guild_id = ? AND message_id = ? AND state = 'open' AND post_message_id IS NOT NULL ORDER BY id LIMIT 1", (draft.guild.id, draft.message_id))
        elif draft.evidence or draft.attachments:
            return None
        else:
            row = await self.db.one("SELECT * FROM reports WHERE guild_id = ? AND target_id = ? AND kind = 'user' AND state = 'open' AND post_message_id IS NOT NULL ORDER BY id LIMIT 1", (draft.guild.id, draft.target.id))
        if row is None or draft.guild.get_channel_or_thread(row["post_channel_id"]) is None:
            return None
        return row

    async def _add_reporter(self, row: dict, draft: Draft) -> bool:
        """Put another reporter on an open report: their name on its post, and no new post, ping or review. False when its post turns out to be gone."""
        reporter = draft.reporter
        also = json.loads(row["also_reported"] or "[]")
        if reporter.id == row["reporter_id"] or any(person["id"] == reporter.id for person in also):
            return True
        count = row["also_count"] + 1
        listed = len(also) < ALSO_MAX
        reason = draft.reason[:REPORT_REASON_MAX]
        if listed:
            also.append({"id": reporter.id, "name": str(reporter), "reason": reason, "at": time.time()})
        fresh = {**row, "also_reported": json.dumps(also), "also_count": count}
        if listed:
            try:
                await self._edit_report_post(fresh)
            except MessageGone:
                await self.db.run("UPDATE reports SET post_message_id = NULL WHERE id = ?", (row["id"],))
                return False
            except OpsError as error:
                log.warning("Could not update the post for report %s: %s", row["id"], error)
        await self.db.run("UPDATE reports SET also_reported = ?, also_count = ? WHERE id = ?", (fresh["also_reported"], count, row["id"]))
        thread = draft.guild.get_thread(row["thread_id"]) if listed and row["thread_id"] else None
        if thread is not None and not thread.archived:  # writing to an archived thread would unarchive it and bump the forum
            await self._post(thread, content=f"Also reported by <@{reporter.id}> ({_name(reporter)}): {_code(reason, REPORT_REASON_MAX)}")
        return True

    async def _file_or_join(self, draft: Draft) -> dict | None:
        """File this report, or add the reporter to the open one it repeats. Returns the new row, or None when it joined one. Two reports of one message take turns here, and the second reads no files."""
        where = draft.message_id if draft.kind == "message" else draft.target.id
        async with self._lock(("filing", draft.guild.id, draft.kind, where)):
            existing = await self._open_duplicate(draft)
            if existing is not None and await self._add_reporter(existing, draft):
                return None
            if draft.attachments:
                draft.evidence = await copy_attachments(draft.attachments, draft.file_limit, self._held)
            return await self.file_report(draft)

    async def _send_report_post(self, channel: Any, row: dict, draft: Draft, whole: str | None) -> Any:
        role_id = self.cfg(draft.guild.id)["alert_role"]
        role = draft.guild.get_role(role_id) if role_id and draft.known and self._ping_free("alert", draft.guild.id) else None

        def build(with_files: bool) -> dict:
            embed = self._report_embed(row, draft, with_files=with_files)
            kwargs: dict[str, Any] = {"embed": embed, "view": controls(report_controls(row))}
            if role is not None:
                kwargs["content"] = role.mention
                kwargs["allowed_mentions"] = discord.AllowedMentions(roles=[role], users=False, everyone=False)
            else:
                kwargs["allowed_mentions"] = discord.AllowedMentions.none()
            if with_files:
                files = [e.file() for e in draft.evidence if e.data is not None]
                if whole and len(files) < FILES_PER_MESSAGE:  # the full text is on the dashboard too, so the evidence comes first
                    files.append(discord.File(io.BytesIO(whole.encode("utf-8")), filename="report.txt"))
                if files:
                    kwargs["files"] = files
            return kwargs

        try:
            return await channel.send(**build(True))
        except discord.HTTPException as error:
            if error.status != 413 and error.code != 40005:  # too much data. Everything but the files still needs to go out.
                raise
            log.warning("Report files were too large for channel %s", channel.id)
            # The post has to say what it really holds, and the same files would be refused again in the review thread.
            self._held.release(draft.evidence)
            body, whole = report_text(draft)
            kept = self._kept_files(draft)
            row.update(post_text=body, files=json.dumps(kept))
            await self.db.run("UPDATE reports SET post_text = ?, files = ? WHERE id = ?", (body, row["files"], row["id"]))
            return await channel.send(**build(False))

    def _report_embed(self, row: dict, draft: Draft, *, with_files: bool) -> discord.Embed:
        embed = discord.Embed(description=_fit(full_report_text(row), 4096), color=STATE_COLORS[row["state"]], timestamp=discord.utils.utcnow())
        embed.set_author(name=report_title(row), icon_url=draft.reporter.display_avatar.url)
        if draft.kind != PLACE:
            embed.set_thumbnail(url=draft.target.display_avatar.url)
        embed.set_footer(text=f"Report {row['number']} | ID: {row['target_id']}")
        for item in draft.evidence:
            if not item.is_image or item.spoiler:
                continue
            if item.data is not None and with_files:
                embed.set_image(url=f"attachment://{item.name}")
            else:
                embed.set_image(url=item.url)
            break
        return embed

    def _reporter_embed(self, draft: Draft) -> discord.Embed:
        target = draft.target
        what = f"[this message]({draft.jump_url}) from {target.mention}" if draft.kind == "message" else target.mention if draft.kind != PLACE else ""
        lead = f"You reported {what} ({_name(target)})." if draft.kind != PLACE else "Your report went to staff."
        embed = discord.Embed(description=f"{lead} Staff have been alerted.\n\n**Reason**\n{_code(draft.reason, REPORT_REASON_MAX)}", color=STATE_COLORS[OPEN])
        embed.set_author(name="Report sent", icon_url=(draft.reporter if draft.kind == PLACE else target).display_avatar.url)
        shown = next((e for e in draft.evidence if e.is_image and not e.spoiler), None) or next((a for a in draft.attachments if (a.content_type or "").startswith("image/") and not a.is_spoiler()), None)
        if shown is not None:
            embed.set_image(url=shown.url)
        return embed

    async def _finish_filing(self, draft: Draft, row: dict) -> None:
        """Everything after the post. A problem here can't undo a report that already reached staff."""
        if not self.cfg(draft.guild.id)["auto_review"] or draft.from_webhook or not draft.known or draft.kind == PLACE:  # a webhook's sender is no one who can be reviewed, and neither is a report that names no one
            return
        try:
            await self.open_review(draft.guild, draft.target, report=row, evidence=draft.evidence, restart=False)
        except Exception:
            log.exception("Could not open the review thread for %s in %s", draft.target.id, draft.guild.id)

    def _message_draft(self, message: Any, reporter: Any, reason: str, attachments: list, file_limit: int) -> Draft:
        content = getattr(message, "clean_content", None) or message.content or ""
        snapshots = getattr(message, "message_snapshots", None) or []  # a forward holds its words and files here, and its own fields are empty
        if not content:
            content = "\n".join(part for part in (getattr(snapshot, "content", "") for snapshot in snapshots) if part)
        embeds = message.embeds or [embed for snapshot in snapshots for embed in getattr(snapshot, "embeds", None) or []]
        if not content and embeds:  # a bot's post is often only an embed, and the report should still show what it said
            first = embeds[0]
            content = "\n".join(part for part in (first.title, first.description) if part) or ""
        return Draft(
            guild=reporter.guild, kind="message", reporter=reporter, target=message.author, reason=reason, channel_id=message.channel.id, message_id=message.id,
            message_created_at=message.created_at.timestamp(), content=content, attachment_count=len(attachments),
            reaction_count=sum(getattr(r, "count", 1) for r in message.reactions), jump_url=message.jump_url, attachments=attachments, file_limit=file_limit, from_webhook=bool(getattr(message, "webhook_id", None)),
        )

    @staticmethod
    def _message_files(message: Any) -> list:
        files = list(message.attachments)
        for snapshot in getattr(message, "message_snapshots", None) or []:
            files.extend(getattr(snapshot, "attachments", None) or [])
        return files

    @app_commands.guild_only()
    async def menu_report_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        refusal = self._refusal(interaction.guild, interaction.user, message.author, noun="messages")
        left = self._cooldown_left(interaction.user)
        if refusal or left:
            await interaction.response.send_message(refusal or self._wait_text(left), ephemeral=True)
            return

        async def submit(answer: discord.Interaction, reason: str) -> None:
            await self.submit_message_report(answer, message, reason)
        await interaction.response.send_modal(ReasonForm("Report Message", submit))

    async def submit_message_report(self, interaction: discord.Interaction, message: Any, reason: str) -> None:
        left = self._cooldown_left(interaction.user)
        if left:
            await interaction.response.send_message(self._wait_text(left), ephemeral=True)
            return
        self._start_cooldown(interaction.user)
        await interaction.response.defer(ephemeral=True)
        limit = getattr(interaction, "filesize_limit", None) or upload_limit(interaction.guild)
        draft = self._message_draft(message, interaction.user, reason, self._message_files(message), limit)
        await self._file_from_interaction(interaction, draft)

    async def _file_from_interaction(self, interaction: discord.Interaction, draft: Draft) -> None:
        try:
            try:
                row = await self._file_or_join(draft)
            except ReportFailed as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            kwargs: dict[str, Any] = {}
            if draft.kind == "message" and draft.jump_url:
                kwargs["view"] = controls([{"type": "link", "url": draft.jump_url, "label": "Jump to message"}])
            await interaction.followup.send(embed=self._reporter_embed(draft), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
            if row is not None:
                await self._finish_filing(draft, row)
        finally:
            self._held.release(draft.evidence)

    @app_commands.guild_only()
    async def menu_report_user(self, interaction: discord.Interaction, member: discord.Member) -> None:
        refusal = self._refusal(interaction.guild, interaction.user, member, noun="profile")
        left = self._cooldown_left(interaction.user)
        if refusal or left:
            await interaction.response.send_message(refusal or self._wait_text(left), ephemeral=True)
            return

        async def submit(answer: discord.Interaction, reason: str) -> None:
            await self.submit_user_report(answer, member, reason)
        await interaction.response.send_modal(ReasonForm("Report User", submit))

    async def submit_user_report(self, interaction: discord.Interaction, member: Any, reason: str, image: Any = None) -> None:
        left = self._cooldown_left(interaction.user)
        if left:
            await interaction.response.send_message(self._wait_text(left), ephemeral=True)
            return
        self._start_cooldown(interaction.user)
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        draft = Draft(
            guild=interaction.guild, kind="user", reporter=interaction.user, target=member, reason=reason, filed_in=channel_id,
            jump_url=_message_url(interaction.guild_id, channel_id, interaction.id) if channel_id else None,
            attachments=[image] if image else [], file_limit=getattr(interaction, "filesize_limit", None) or upload_limit(interaction.guild),
        )
        await self._file_from_interaction(interaction, draft)

    @staticmethod
    async def _message_before(ctx: commands.Context) -> int:
        """The message above a typed report, which is what it's about. The report itself comes down, so a link to it would lead nowhere."""
        try:
            async for message in ctx.channel.history(limit=1, before=ctx.message):
                return message.id
        except discord.HTTPException:
            pass
        return ctx.message.id

    async def submit_place_report(self, interaction: discord.Interaction, reason: str, image: Any = None) -> None:
        """A report that names no one, carrying the channel it was filed in."""
        left = self._cooldown_left(interaction.user)
        if left:
            await interaction.response.send_message(self._wait_text(left), ephemeral=True)
            return
        self._start_cooldown(interaction.user)
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        draft = Draft(
            guild=interaction.guild, kind=PLACE, reporter=interaction.user, target=NO_ONE_NAMED, reason=reason, filed_in=channel_id,
            jump_url=_message_url(interaction.guild_id, channel_id, interaction.id) if channel_id else None,
            attachments=[image] if image else [], file_limit=getattr(interaction, "filesize_limit", None) or upload_limit(interaction.guild),
        )
        await self._file_from_interaction(interaction, draft)

    @commands.hybrid_command(name="report", description="Report a member or a problem to the staff")
    @commands.guild_only()
    @app_commands.describe(member="The member you are reporting. Leave it out when no one in particular is to blame.", reason="What happened. Leave it out to write it in a form.", image="An image for the staff to see")
    async def report(self, ctx: commands.Context, member: discord.Member | discord.User | None = None, *, reason: str | None = None, image: discord.Attachment | None = None) -> None:
        refusal = self._refusal(ctx.guild, ctx.author, member or NO_ONE_NAMED, noun="profile")
        left = self._cooldown_left(ctx.author)
        if ctx.interaction is not None:
            if refusal or left:
                await ctx.fail(refusal or self._wait_text(left))
                return
            if member is None and reason:
                await self.submit_place_report(ctx.interaction, reason, image)
                return
            if member is None:
                async def submit_place(answer: discord.Interaction, written: str) -> None:
                    await self.submit_place_report(answer, written, image)
                await ctx.interaction.response.send_modal(ReasonForm("Report", submit_place))
                return
            if not reason:
                async def submit(answer: discord.Interaction, written: str) -> None:
                    await self.submit_user_report(answer, member, written, image)
                await ctx.interaction.response.send_modal(ReasonForm("Report User", submit))
                return
            await self.submit_user_report(ctx.interaction, member, reason, image)
            return
        # A report is private and a text command is typed in the open, so it comes down at once. Only reading its image goes first,
        # since that stops working once the message is gone. The cooldown starts before anything waits, so a burst of messages can't all pass it.
        refusal = refusal or left and self._wait_text(left) or (None if reason else ("Write what happened after their name." if member is not None else "Write what happened."))
        if not refusal:
            self._start_cooldown(ctx.author)
        evidence = [] if refusal else await copy_attachments(list(ctx.message.attachments)[:1], upload_limit(ctx.guild), self._held)
        jump_to = ctx.message.id if member is not None else await self._message_before(ctx)
        removed = await _delete_quietly(ctx.message)
        if refusal:
            await ctx.fail(refusal, delete_after=10)
            return
        draft = Draft(
            guild=ctx.guild, kind="user" if member is not None else PLACE, reporter=ctx.author, target=member or NO_ONE_NAMED, reason=reason, filed_in=ctx.channel.id,
            jump_url=_message_url(ctx.guild.id, ctx.channel.id, jump_to), evidence=evidence,
        )
        await self._file_typed(ctx, draft, removed)

    async def _file_typed(self, ctx: commands.Context, draft: Draft, removed: bool) -> None:
        """The rest of a typed report once its message is down: file it, answer the reporter in private, and open the review."""
        try:
            try:
                row = await self._file_or_join(draft)
            except ReportFailed as error:
                await ctx.fail(str(error), delete_after=10)
                return
            if not removed:
                await ctx.fail("Your report went to staff, but I can't remove your message here. Delete it yourself, or use the slash command next time.", delete_after=15)
            try:
                await ctx.author.send(embed=self._reporter_embed(draft), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:  # their DMs are closed, so a short note in the channel has to do
                await ctx.send("Report sent.", delete_after=8)
            if row is not None:
                await self._finish_filing(draft, row)
        finally:
            self._held.release(draft.evidence)

    # ---- voice reports, which the voice module's /vc_report and VC Report menu file here like any other report ----

    def voice_refusal(self, reporter: Any, target: Any) -> str | None:
        """Why this voice report can't be filed now. The rules and the cooldown are the ones every report follows."""
        refusal = self._refusal(reporter.guild, reporter, target, noun="profile")
        if refusal:
            return refusal
        left = self._cooldown_left(reporter)
        return self._wait_text(left) if left else None

    @staticmethod
    def voice_draft(reporter: Any, target: Any, reason: str, *, filed_in: int | None, jump_url: str | None) -> Draft:
        """A report on someone in voice, holding the channel they were in and who was there, as they were when it was filed."""
        voice = getattr(target, "voice", None)
        channel = voice.channel if voice is not None else None
        present = [discord.utils.escape_markdown(member.display_name) for member in channel.members if not member.bot] if channel is not None else []
        return Draft(
            guild=reporter.guild, kind=VOICE, reporter=reporter, target=target, reason=reason, channel_id=channel.id if channel is not None else None,
            in_voice=present, filed_in=filed_in, jump_url=jump_url,
        )

    async def submit_voice_report(self, interaction: discord.Interaction, member: Any, reason: str) -> None:
        """A voice report from the slash command or the menu's form, answered and filed as a member report is."""
        left = self._cooldown_left(interaction.user)
        if left:
            await interaction.response.send_message(self._wait_text(left), ephemeral=True)
            return
        self._start_cooldown(interaction.user)
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        draft = self.voice_draft(interaction.user, member, reason, filed_in=channel_id, jump_url=_message_url(interaction.guild_id, channel_id, interaction.id) if channel_id else None)
        await self._file_from_interaction(interaction, draft)

    async def typed_voice_report(self, ctx: commands.Context, member: Any, reason: str) -> None:
        """A voice report typed as text. Like a typed report, its message comes down at once and the answer is private."""
        refusal = self.voice_refusal(ctx.author, member)
        if not refusal:
            self._start_cooldown(ctx.author)
        removed = await _delete_quietly(ctx.message)
        if refusal:
            await ctx.fail(refusal, delete_after=10)
            return
        draft = self.voice_draft(ctx.author, member, reason, filed_in=ctx.channel.id, jump_url=_message_url(ctx.guild.id, ctx.channel.id, ctx.message.id))
        await self._file_typed(ctx, draft, removed)

    @report.error
    async def _report_refused(self, ctx: commands.Context, error: Exception) -> None:
        """A typed report that fails before the command's body would stay up in public, naming the person it accuses, under a public error."""
        if ctx.interaction is not None or not isinstance(error, (commands.CheckFailure, commands.UserInputError, commands.CommandOnCooldown, commands.DisabledCommand)):
            return
        ctx.error_answered = True
        await _delete_quietly(ctx.message)
        await ctx.fail(plain_error(ctx, error), delete_after=10)

    @commands.hybrid_command(name="reportchannel", description="Set the channel where reports are sent")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="The channel for reports")
    async def reportchannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        refusal = access.post_refusal(ctx.author, channel)
        if refusal is None and any(self.pinged_channels({**self.cfg(ctx.guild.id), "report_channel": channel.id}).values()):
            refusal = access.ping_refusal(ctx.author, channel)
        if refusal:
            await ctx.fail(refusal)
            return
        await self.bot.settings.save(ctx.guild.id, self.key, {"report_channel": channel.id}, actor=self.bot.actor(ctx.author), action="reports.channel")
        await ctx.ok(f"Reports now go to {channel.mention}.")

    # ---- report buttons ----

    async def report_by_id(self, guild_id: int, report_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM reports WHERE guild_id = ? AND id = ?", (guild_id, report_id))

    async def report_by_post(self, guild_id: int, message_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM reports WHERE guild_id = ? AND post_message_id = ?", (guild_id, message_id))

    async def change_state(self, row: dict, state: str, actor: Any) -> dict | None:
        """Claim the change so two moderators pressing together can't both win. Returns the new row, or None if someone got there first."""
        if state == OPEN:
            changed = await self.db.run(
                "UPDATE reports SET state = 'open', handled_by = NULL, handled_name = NULL, handled_at = NULL, case_number = NULL, reset_by = ?, reset_name = ? WHERE id = ? AND state != 'open'",
                (actor.id, str(getattr(actor, "name", actor)), row["id"]),
            )
        else:
            changed = await self.db.run(
                "UPDATE reports SET state = ?, handled_by = ?, handled_name = ?, handled_at = ?, reset_by = NULL, reset_name = NULL WHERE id = ? AND state = 'open'",
                (state, actor.id, str(getattr(actor, "name", actor)), time.time(), row["id"]),
            )
        if not changed:
            return None
        fresh = await self.report_by_id(row["guild_id"], row["id"])
        if state == ACTIONED and not fresh["case_number"]:
            fresh = await self._link_newest_case(fresh)
        return fresh

    async def _link_newest_case(self, row: dict) -> dict:
        """Staff act on a report and then press Actioned, so a case made for this person since the report is almost always the one. The unmute when a mute runs out isn't what they did about it."""
        record = await self._member_record(row["guild_id"], row["target_id"])
        for case in record["cases"]:
            if (case.get("created_at") or 0) >= row["created_at"] and case.get("number") and case.get("action") not in UNDO_ACTIONS:
                await self.db.run("UPDATE reports SET case_number = ? WHERE id = ?", (case["number"], row["id"]))
                return {**row, "case_number": case["number"]}
        return row

    def _handles(self, member: Any, row: dict | None) -> bool:
        """Who may press a report's buttons: staff, and on a VC report the server's voice moderators too, who handled VC reports before they were reports."""
        if self.is_staff(member):
            return True
        voice = self.bot.module("voice")
        return row is not None and row["kind"] == VOICE and voice is not None and voice.is_moderator(member)

    async def press_state(self, interaction: discord.Interaction, state: str) -> None:
        row = await self.report_by_post(interaction.guild_id, interaction.message.id)
        if not self._handles(interaction.user, row):
            await interaction.response.send_message("Only staff can handle reports.", ephemeral=True)
            return
        if row is None:
            await interaction.response.send_message("This report is no longer tracked.", ephemeral=True)
            return
        refusal = self._subject_refusal(interaction.user, row["target_id"])
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        fresh = await self.change_state(row, state, interaction.user)
        if fresh is None:
            await interaction.response.send_message("Someone else already handled this report.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self._restyled(interaction.message, fresh), view=controls(report_controls(fresh)))

    @staticmethod
    def _restyled(message: Any, row: dict) -> discord.Embed:
        """The post's embed with its text and color brought up to date. Its image and thumbnail stay as Discord holds them."""
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.description = _fit(full_report_text(row), 4096)
        embed.color = STATE_COLORS[row["state"]]
        return embed

    async def press_history(self, interaction: discord.Interaction) -> None:
        row = await self.report_by_post(interaction.guild_id, interaction.message.id)
        if not self._handles(interaction.user, row):
            await interaction.response.send_message("Only staff can read message history.", ephemeral=True)
            return
        if row is None:
            await interaction.response.send_message("This report is no longer tracked.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        answer = await self.history_answer(interaction.guild, interaction.user, row["target_id"], row["target_name"], channel_id=row["channel_id"])
        await interaction.followup.send(ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **answer)

    def _can_read(self, guild: Any, viewer: Any, channel_id: int) -> bool:
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return bool(viewer.guild_permissions.administrator)
        return access.can_read(viewer, channel)

    def _may_gather_from(self, guild: Any, viewer: Any, channel_id: int) -> bool:
        """Gathering copies messages into a forum thread other staff read, so the person asking must be able to read the channel, and so must the moderator role."""
        if not self._can_read(guild, viewer, channel_id):
            return False
        role_id = self.cfg(guild.id)["moderator_role"]
        role = guild.get_role(role_id) if role_id else None
        channel = guild.get_channel_or_thread(channel_id)
        if role is None or channel is None:
            return True
        allowed = access.permissions_in(role, channel)
        return bool(allowed.view_channel and (channel.type != discord.ChannelType.private_thread or allowed.manage_threads))

    async def _recent_in_channel(self, guild: Any, channel_id: int, user_id: int) -> list[dict]:
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None or not hasattr(channel, "history"):
            return []
        return [_stored_form(message) for message in await self._read_channel(channel, user_id, None)][:HISTORY_LIMIT]

    async def history_answer(self, guild: Any, viewer: Any, user_id: int, user_name: str, *, channel_id: int | None = None) -> dict:
        """A member's recent messages from the store, limited to channels the person asking can read.

        The store holds nothing from bots or from before it started. When it comes up empty, the channel the reported message is in (`channel_id`) stands in.
        """
        reader = getattr(self.bot.module("logging"), "user_messages", None)
        if reader is None:
            return {"content": "The message store isn't available, so there's no history to show."}
        rows = [r for r in await reader(guild.id, user_id, limit=HISTORY_LIMIT) if self._can_read(guild, viewer, r["channel_id"])]
        if not rows and channel_id and self._can_read(guild, viewer, channel_id):
            rows = await self._recent_in_channel(guild, channel_id, user_id)
        name = discord.utils.escape_markdown(user_name or str(user_id))
        head = f"{_many(len(rows), 'recent message')} from <@{user_id}> ({name}) in {discord.utils.escape_markdown(guild.name)}."
        if not rows:
            return {"embed": discord.Embed(description=head, color=STATE_COLORS[OPEN])}
        shown, plain = [], []
        room = 3900 - len(head)
        all_fit = True
        for row in rows:
            created = _short_stamp(row.get("created_at"))
            marks = []
            if row.get("edited_at"):
                marks.append("edited")
            if row.get("deleted_at"):
                marks.append("deleted")
            tag = f" ({', '.join(marks)})" if marks else ""
            files = row.get("attachments") or []
            extra = f"\nFiles: {len(files)}" if files else ""
            url = _message_url(guild.id, row["channel_id"], row["id"])
            entry = f"[Message]({url}) in <#{row['channel_id']}>, {created}{tag}{extra}\nContent: {_code(row.get('content'), 300)}\n"
            if len(entry) + 1 <= room and all_fit:
                shown.append(entry)
                room -= len(entry) + 1
            else:
                all_fit = False
            names = ", ".join(f.get("filename", "file") for f in files)
            plain.append(f"- Message {row['id']}\n  Channel ID: {row['channel_id']}\n  Created: {_utc_text(row.get('created_at'))}{tag}\n  Files: {names or 'none'}\n  Content: {row.get('content') or ''}\n")
        answer: dict[str, Any] = {"embed": discord.Embed(description=head + "\n\n" + "\n".join(shown), color=STATE_COLORS[OPEN], timestamp=discord.utils.utcnow())}
        if not all_fit:
            answer["file"] = discord.File(io.BytesIO(("\n".join(plain)).encode("utf-8")), filename="recent_messages.txt")
        return answer

    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def menu_reset_report(self, interaction: discord.Interaction, message: discord.Message) -> None:
        permissions = interaction.user.guild_permissions
        if not (permissions.manage_guild or permissions.administrator):
            await interaction.response.send_message("You need Manage Server to reset a report.", ephemeral=True)
            return
        row = await self.report_by_post(interaction.guild_id, message.id)
        if row is None:
            await interaction.response.send_message("This only works on a report post.", ephemeral=True)
            return
        refusal = self._subject_refusal(interaction.user, row["target_id"])
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        fresh = await self.change_state(row, OPEN, interaction.user)
        if fresh is None:
            await interaction.response.send_message("This report hasn't been marked yet.", ephemeral=True)
            return
        await message.edit(embed=self._restyled(message, fresh), view=controls(report_controls(fresh)))
        await interaction.response.send_message("This report has been reset.", ephemeral=True)

    async def sync_report_post(self, row: dict) -> None:
        """Bring a report's post up to date from its row, for changes that didn't come from a button on the post."""
        if not row["post_message_id"]:
            return
        try:
            await self._edit_report_post(row)
        except OpsError as error:
            log.warning("Could not update the post for report %s: %s", row["id"], error)

    async def _edit_report_post(self, row: dict) -> None:
        """Raises MessageGone when the post is gone, and OpsError for any other reason Discord refuses."""
        ops = self.bot.ops
        message = await ops.fetch_message(row["post_channel_id"], row["post_message_id"])
        payload = dict(message["payload"])
        embeds = list(payload.get("embeds") or [])
        if embeds:
            embeds[0] = {**embeds[0], "description": _fit(full_report_text(row), 4096), "color": STATE_COLORS[row["state"]]}
        payload["embeds"] = embeds
        payload["buttons"] = []  # the jump link is one of the controls below, in its usual place
        await ops.edit_message(row["post_channel_id"], row["post_message_id"], payload, components=report_controls(row))

    # ---- review threads: finding them ----

    async def _index_thread(self, guild_id: int, user_id: int, thread_id: int, user_name: str | None) -> None:
        await self.db.run(
            "INSERT INTO review_threads (guild_id, user_id, user_name, thread_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET user_name = excluded.user_name, thread_id = excluded.thread_id, "
            "stats_message_id = CASE WHEN review_threads.thread_id = excluded.thread_id THEN review_threads.stats_message_id ELSE NULL END",
            (guild_id, user_id, user_name, thread_id),
        )

    async def load_thread_index(self, guild: Any) -> int:
        """Find threads named `... UID n` in the review forum, active and archived, so a member's old thread gets reused."""
        forum = self._forum(guild)
        self._forums[guild.id] = forum.id if forum else None
        if forum is None:
            return 0
        found: dict[int, Any] = {}

        def note(thread: Any) -> None:
            user_id = uid_in_name(thread.name)
            if user_id is not None:
                found.setdefault(user_id, thread)  # first seen wins, and active threads come first, so a live thread beats an old archived one

        for thread in sorted(forum.threads, key=lambda t: t.id, reverse=True):
            note(thread)
        try:
            async for thread in forum.archived_threads(limit=None):
                note(thread)
        except discord.HTTPException as error:
            log.warning("Could not read archived threads in %s: %s", guild.id, error)
        for user_id, thread in found.items():
            await self._index_thread(guild.id, user_id, thread.id, name_in_thread(thread.name))
        log.info("Review threads in %s: %d found", guild.id, len(found))
        return len(found)

    async def _settings_saved(self, guild_id: int, after: dict) -> None:
        if after["forum_channel"] == self._forums.get(guild_id):
            return
        self._forums[guild_id] = after["forum_channel"]
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            self._spawn(self.load_thread_index(guild))

    @commands.Cog.listener()
    async def on_thread_create(self, thread: Any) -> None:
        if thread.parent_id != self.cfg(thread.guild.id)["forum_channel"]:
            return
        user_id = uid_in_name(thread.name)
        if user_id is not None:
            await self._index_thread(thread.guild.id, user_id, thread.id, name_in_thread(thread.name))

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: Any) -> None:
        await self.db.run("DELETE FROM review_threads WHERE guild_id = ? AND thread_id = ?", (payload.guild_id, payload.thread_id))
        await self.db.run("DELETE FROM review_state WHERE thread_id = ?", (payload.thread_id,))
        await self.db.run("UPDATE reports SET thread_id = NULL WHERE guild_id = ? AND thread_id = ?", (payload.guild_id, payload.thread_id))

    async def _existing_thread(self, guild: Any, forum: Any, user_id: int) -> Any:
        """A member's thread in this forum, unarchived and ready. None means they have none. A failed lookup raises, because creating a second thread is worse."""
        row = await self.db.one("SELECT thread_id FROM review_threads WHERE guild_id = ? AND user_id = ?", (guild.id, user_id))
        if row is None:
            return None
        thread = guild.get_thread(row["thread_id"])
        if thread is None:
            try:
                thread = await guild.fetch_channel(row["thread_id"])
            except discord.NotFound:
                await self.db.run("DELETE FROM review_threads WHERE guild_id = ? AND user_id = ?", (guild.id, user_id))
                return None
            except discord.HTTPException as error:
                raise ThreadLookupFailed(str(error)) from error
        if not is_thread(thread):
            await self.db.run("DELETE FROM review_threads WHERE guild_id = ? AND user_id = ?", (guild.id, user_id))
            return None
        if thread.parent_id != forum.id:
            return None  # the server moved its review forum, so reviews belong in the new one, and the new thread takes over this row
        if thread.archived:  # a lock stays on, since someone put it there on purpose
            try:
                thread = await thread.edit(archived=False)
            except discord.HTTPException as error:
                log.warning("Could not unarchive thread %s: %s", thread.id, error)
        return thread

    async def _create_thread(self, guild: Any, forum: Any, user: Any) -> Any:
        title = _fit(f"{user.name} UID {user.id}", THREAD_NAME_MAX)
        kwargs: dict[str, Any] = {}
        flags = getattr(forum, "flags", None)
        tags = getattr(forum, "available_tags", None) or []
        if tags and flags is not None and flags.require_tag:  # a forum that insists on a tag refuses a post without one
            kwargs["applied_tags"] = [tags[0]]
        stats = await self._stats_text(guild, user.id, user.name)
        made = await forum.create_thread(name=title, content=stats, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        await self._index_thread(guild.id, user.id, made.thread.id, user.name)
        await self.db.run("UPDATE review_threads SET stats_message_id = ? WHERE guild_id = ? AND user_id = ?", (made.message.id, guild.id, user.id))
        return made.thread

    # ---- review threads: the stats post ----

    async def _stats_text(self, guild: Any, user_id: int, user_name: str | None) -> str:
        member = guild.get_member(user_id)
        record = await self._member_record(guild.id, user_id)
        counts = record["counts"]
        pings = await self.db.scalar("SELECT count FROM moderator_pings WHERE guild_id = ? AND user_id = ?", (guild.id, user_id)) or 0
        reports = await self.db.one("SELECT COUNT(*) AS amount, COALESCE(SUM(state = 'open'), 0) AS still_open FROM reports WHERE guild_id = ? AND target_id = ?", (guild.id, user_id))
        created = member.created_at if member else discord.utils.snowflake_time(user_id)
        joined = _short_stamp(member.joined_at) if member and member.joined_at else "Not in the server now"
        head = (
            f"**User:** <@{user_id}> (`{user_id}`)\n"
            f"**Account Creation Date:** {_short_stamp(created)}\n"
            f"**Server Join Date:** {joined}\n"
            f"**Report Count:** {reports['amount']} ({reports['still_open']} open)\n"
            f"**Moderator Ping Count:** {pings}\n"
            f"**Warn Count:** {counts['warn']}\n"
            f"**Mute Count:** {counts['mute']}\n"
            f"**Tempban Count:** {counts['tempban']}\n"
            f"**Timeout Count:** {counts['timeout']}\n"
            f"**Kick Count:** {counts['kick']}\n"
            f"**Ban Count:** {counts['ban']}\n"
            "**Mute Details:**\n"
        )
        mutes = [c for c in record["cases"] if c.get("action") in ("mute", "hardmute")]
        if not mutes:
            return head + "No mutes recorded."
        lines: list[str] = []
        room = 1990 - len(head)
        for index, case in enumerate(mutes):
            line = f"Date: {_short_stamp(case.get('created_at'))}, Duration: {rounded_duration(int(case.get('seconds') or 0))}"
            if len(line) + 30 > room:
                lines.append(f"And {len(mutes) - index} more.")
                break
            lines.append(line)
            room -= len(line) + 1
        return head + "\n".join(lines)

    async def _refresh_stats(self, thread: Any, guild: Any, user_id: int, user_name: str | None) -> None:
        """Editing doesn't bump a forum thread, so the top post stays current without noise. In a thread an earlier bot started, ours is a new post."""
        row = await self.db.one("SELECT stats_message_id FROM review_threads WHERE guild_id = ? AND user_id = ?", (guild.id, user_id))
        content = await self._stats_text(guild, user_id, user_name)
        message_id = row["stats_message_id"] if row else None
        if message_id:
            try:
                await thread.get_partial_message(message_id).edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as error:
                log.warning("Could not update the stats post in thread %s: %s", thread.id, error)
                return
        posted = await self._post(thread, content=content)
        if posted is not None:
            await self.db.run("UPDATE review_threads SET stats_message_id = ? WHERE guild_id = ? AND user_id = ?", (posted.id, guild.id, user_id))

    # ---- review threads: opening one ----

    async def _post(self, thread: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        try:
            return await thread.send(**kwargs)
        except discord.HTTPException as error:
            log.warning("Could not post in thread %s: %s", thread.id, error)
            return None

    async def _post_embeds(self, thread: Any, embeds: list[discord.Embed]) -> int:
        """Up to ten embeds and Discord's character limit per message, so a long history doesn't take one message per entry. Returns how many entries Discord refused."""
        batch: list[discord.Embed] = []
        size = 0
        refused = 0
        for embed in embeds:
            entry = len(embed)
            if batch and (len(batch) >= 10 or size + entry > 5500):
                if await self._post(thread, embeds=batch) is None:
                    refused += len(batch)
                await asyncio.sleep(POST_PAUSE)
                batch, size = [], 0
            batch.append(embed)
            size += entry
        if batch and await self._post(thread, embeds=batch) is None:
            refused += len(batch)
        return refused

    async def open_review(self, guild: Any, user: Any, *, report: dict | None = None, evidence: list[Evidence] | None = None, restart: bool = True) -> Any:
        """Open this member's review, in their thread or a new one, and put the checklist at step one. Returns the thread, or None if no forum is usable.

        With `restart` off, for a report coming in, a review still running keeps its step and gets no new ping. The moderator on it reads the new report in the thread.
        """
        forum = self._forum(guild)
        if forum is None:
            return None
        cfg = self.cfg(guild.id)
        # A lock per member, so two reports arriving together can't each create a thread.
        async with self._lock(("member", guild.id, user.id)):
            try:
                thread = await self._existing_thread(guild, forum, user.id)
            except ThreadLookupFailed as error:
                log.warning("Skipped the review thread for %s in %s: %s", user.id, guild.id, error)
                return None
            if thread is None:
                try:
                    thread = await self._create_thread(guild, forum, user)
                except discord.HTTPException as error:
                    log.warning("Could not create a review thread for %s in %s: %s", user.id, guild.id, error)
                    return None
                state = None
            else:
                state = await self._state(thread.id)
                if state is None or not state["modal_message_id"]:
                    await self._retire_foreign_checklists(thread)
                await self._refresh_stats(thread, guild, user.id, user.name)
            running = not restart and state is not None and state["status"] == ACTIVE
            role = guild.get_role(cfg["moderator_role"]) if cfg["moderator_role"] else None
            if role is not None and not running and (restart or self._ping_free("moderator", guild.id)):
                await self._post(thread, content=role.mention, allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False))
            if report is not None:
                await self._post_report_copy(thread, guild, user, report, evidence or [])
                await self.db.run("UPDATE reports SET thread_id = ? WHERE id = ?", (thread.id, report["id"]))
            if running:
                if report is not None and not state["report_id"]:
                    await self._update_state(thread.id, report_id=report["id"])
                await self.show_step(thread, state["current_step"], expected=state["current_step"])  # the same step, back at the bottom under the new report
                return thread
            # A new report reopens the checklist at step one when an earlier review here was finished or dismissed.
            await self._begin_review(thread.id, guild.id, user.id, report["id"] if report else None)
            await self.show_step(thread, 0)
        return thread

    async def _post_report_copy(self, thread: Any, guild: Any, user: Any, report: dict, evidence: list[Evidence]) -> None:
        embed = discord.Embed(description=_fit(full_report_text(report), 4096), color=STATE_COLORS[report["state"]], timestamp=discord.utils.utcnow())
        embed.set_author(name=report_title(report))
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"Report {report['number']} | ID: {user.id}")
        await self._post(thread, embed=embed)
        if len(report["reason"] or "") > REPORT_REASON_MAX:  # the copy above holds only the start of it, and the thread is where staff read
            await self._post_lines(thread, ["**Full reason:**", report["reason"]])
        files = [e.file() for e in evidence if e.data is not None][:FILES_PER_MESSAGE]
        if report["kind"] != "message":
            if files:
                await self._post(thread, content="Image sent with this report:", files=files)
            return
        link = f"[Link to message]({report['jump_url']})"
        head = f"**Reported message from <@{report['target_id']}> in <#{report['channel_id']}>:**\n"
        body = _fit(report["content"] or "", 2000 - len(head) - len(link) - 2)
        kwargs: dict[str, Any] = {"content": f"{head}{body}\n{link}"}
        if files:
            kwargs["files"] = files
        if await self._post(thread, **kwargs) is None and files:
            await self._post(thread, content=f"{head}{body}\n{link}")  # the files were probably too large, and the text still belongs here
        entry = await self._log_entry_for_message(guild, report["target_id"], report["message_id"])
        if entry is not None:
            await self._post(thread, embed=entry)

    async def _log_entry_for_message(self, guild: Any, user_id: int, message_id: int | None) -> discord.Embed | None:
        """The log card for this message, from the store, or from the older log channel for what happened before the store did."""
        if not message_id:
            return None
        reader = getattr(self.bot.module("logging"), "user_log_entries", None)
        for entry in await reader(guild.id, user_id, limit=HISTORY_LIMIT, kind="message") if reader else []:
            data = entry.get("embed")
            # The column, never the text: a member's own words in an entry could name any message.
            if isinstance(data, dict) and entry.get("message_id") == message_id:
                return discord.Embed.from_dict(data)
        channel_id = self.cfg(guild.id)["log_channel_fallback"]
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        if channel is None or not hasattr(channel, "history"):
            return None
        try:
            async for message in channel.history(limit=LOG_CARD_SCAN):
                for embed in message.embeds[:1]:
                    # This bot's own cards hold a member's text in the description and the ID in a field, so only an older card's description is read.
                    description = None if any(field.name == "Message ID" for field in embed.fields) else embed.description
                    if _is_card_for(embed.url, description, message_id):
                        return embed
        except discord.HTTPException as error:
            log.warning("Could not read log channel %s: %s", channel_id, error)
        return None

    async def _retire_foreign_checklists(self, thread: Any) -> None:
        """A thread taken over from an earlier bot may still hold its checklists, whose buttons no longer answer. They go, the first time only."""
        me = thread.guild.me.id
        try:
            async for message in thread.history(limit=200):
                if not message.author.bot or message.author.id == me or not message.embeds:
                    continue
                embed = message.embeds[0]
                if STEP_TITLE.match(embed.title or "") and (embed.description or "") in REVIEW_STEPS:
                    await message.delete()
                    await asyncio.sleep(DELETE_PAUSE)
        except discord.Forbidden:
            return  # without Manage Messages they stay, and everything else still works
        except discord.HTTPException as error:
            log.warning("Could not clear old checklists from thread %s: %s", thread.id, error)

    @commands.hybrid_command(name="review", description="Open a member's review thread and start its checklist")
    @commands.guild_only()
    @app_commands.describe(member="The member to review. Works for people who have left.")
    @staff_only("Staff")
    async def review(self, ctx: commands.Context, member: discord.User) -> None:
        if not self.is_staff(ctx.author):
            await ctx.fail("Only staff can open a review.")
            return
        refusal = self._subject_refusal(ctx.author, member.id)
        if refusal:
            await ctx.fail(refusal)
            return
        if self._forum(ctx.guild) is None:
            await ctx.fail("No review forum is set. Choose one on the Reports page.")
            return
        await ctx.defer(ephemeral=True)
        thread = await self.open_review(ctx.guild, member)
        if thread is None:
            await ctx.fail("The thread couldn't be opened. Check the bot's permissions in the review forum, then try again.")
            return
        await ctx.ok(f"The review is open in {thread.mention}.")

    async def open_review_by_id(self, guild_id: int, user_id: int, report_id: int | None = None) -> dict:
        """The dashboard's way in. Returns where the thread is."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise OpsError("The bot isn't in that server.")
        if self._forum(guild) is None:
            raise OpsError("No review forum is set. Choose one in the settings on this page.")
        user = guild.get_member(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException as error:
                raise OpsError("Discord doesn't know that person.") from error
        report = await self.report_by_id(guild_id, report_id) if report_id else None
        thread = await self.open_review(guild, user, report=report)
        if thread is None:
            raise OpsError("The thread couldn't be opened. Check the bot's permissions in the review forum, then try again.")
        return {"thread_id": thread.id, "url": f"https://discord.com/channels/{guild_id}/{thread.id}"}

    # ---- review threads: state ----

    async def _state(self, thread_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM review_state WHERE thread_id = ?", (thread_id,))

    async def _begin_review(self, thread_id: int, guild_id: int, user_id: int, report_id: int | None) -> None:
        """Begin or restart a review. This is the only path that creates a row. An existing checklist ID stays, so show_step can replace that message."""
        await self.db.run(
            "INSERT INTO review_state (thread_id, guild_id, user_id, current_step, status, report_id, updated_at) VALUES (?, ?, ?, 0, 'active', ?, ?) "
            "ON CONFLICT (thread_id) DO UPDATE SET current_step = 0, status = 'active', report_id = excluded.report_id, updated_at = excluded.updated_at",
            (thread_id, guild_id, user_id, report_id, time.time()),
        )

    async def _update_state(self, thread_id: int, **fields_: Any) -> None:
        """Update an existing row and never create one, so a review that finished can't be brought back by a write already in flight."""
        allowed = ("current_step", "modal_message_id", "status", "report_id")
        names = [name for name in fields_ if name in allowed]
        if not names:
            return
        assignments = ", ".join(f"{name} = ?" for name in names)
        await self.db.run(f"UPDATE review_state SET {assignments}, updated_at = ? WHERE thread_id = ?", [*(fields_[n] for n in names), time.time(), thread_id])

    async def _fetch_tracked(self, thread: Any, state: dict) -> tuple[Any, bool]:
        """The checklist this thread owns, and whether we really know. False means the lookup failed for a reason other than the message
        being gone, so the caller must not read that as "it disappeared" and post a replacement."""
        message_id = state["modal_message_id"]
        if not message_id:
            return None, True
        try:
            return await thread.fetch_message(message_id), True
        except discord.NotFound:
            return None, True
        except discord.HTTPException as error:
            log.warning("Could not check checklist %s in thread %s: %s", message_id, thread.id, error)
            return None, False

    async def _step_embed(self, guild: Any, state: dict, step: int) -> discord.Embed:
        embed = discord.Embed(title=f"Step {step + 1}", description=REVIEW_STEPS[step], color=discord.Color.blue())
        user_id = state["user_id"]
        cfg = self.cfg(guild.id)
        if step == 1:
            record = await self._member_record(guild.id, user_id)
            moderation = self.bot.module("moderation")
            mute_default = moderation.cfg(guild.id)["mute"]["seconds"] if moderation is not None else 0
            embed.add_field(name="Moderation Recommendation", value=propose_action(cfg["ladder"], record["counts"], f"<@{user_id}>", mute_default), inline=False)
        elif step == 2:
            embed.add_field(name="Create Ticket", value=_fit(cfg["ticket_command"].replace("{user}", f"<@{user_id}>"), 1024) or "Open a ticket for this member.")
        elif step == 3:
            embed.add_field(name="Acknowledge Report", value=await self._acknowledge_text(guild, state))
        return embed

    async def _acknowledge_text(self, guild: Any, state: dict) -> str:
        report = await self.report_by_id(guild.id, state["report_id"]) if state["report_id"] else None
        channel_id = self.cfg(guild.id)["report_channel"]
        if report is None:
            where = f" <#{channel_id}>" if channel_id else ""
            return f"Please acknowledge the user report in the reported messages channel{where} if not already done."
        others = [row for row in await self._open_in_review(guild.id, state) if row["id"] != report["id"]]
        url = _message_url(guild.id, report["post_channel_id"], report["post_message_id"]) if report["post_message_id"] else None
        jump = f"[Jump to report {report['number']}]({url})" if url else f"Report {report['number']}"
        more = f" {_many(len(others), 'other report')} on this member {'is' if len(others) == 1 else 'are'} open too." if others else ""
        if report["state"] != OPEN:
            if others:
                return f"{jump} has already been marked by {discord.utils.escape_markdown(report['handled_name'] or 'staff')}.{more} Acknowledge them here, or from the reports channel."
            return f"{jump} has already been marked by {discord.utils.escape_markdown(report['handled_name'] or 'staff')}. Mark this step completed to continue."
        return f"{jump} is still open.{more} Acknowledge {'them' if others else 'it'} here, or from the reports channel."

    async def _open_in_review(self, guild_id: int, state: dict) -> list[dict]:
        """Every report still open that this review covers. More can arrive while it runs, and one left behind would stay open forever. A review staff opened by hand covers none."""
        if not state["report_id"]:
            return []
        return await self.db.all("SELECT * FROM reports WHERE guild_id = ? AND state = 'open' AND (thread_id = ? OR id = ?) ORDER BY id", (guild_id, state["thread_id"], state["report_id"]))

    async def _report_is_open(self, guild_id: int, state: dict) -> bool:
        return bool(await self._open_in_review(guild_id, state))

    async def show_step(self, thread: Any, step: int, *, expected: int | None = None) -> bool:
        """Move a review to `step`, as this thread's one checklist message. `expected` is the step the click came from, so a second click
        that lost the race does nothing. The step number is recorded inside the lock, so no separate write can race with a click.
        False means Discord refused the post, so the review stayed where it was."""
        async with self._thread_lock(thread.id):
            state = await self._state(thread.id)
            if state is None or state["status"] != ACTIVE:
                return True
            if expected is not None and state["current_step"] != expected:
                return True
            if step >= len(REVIEW_STEPS):
                await self._close_review(thread, state)
                return True
            return await self._post_checklist(thread, state, step)

    async def _post_checklist(self, thread: Any, state: dict, step: int) -> bool:
        """Runs under the thread's lock. The replacement goes out first, so a failed post leaves the old one in place."""
        previous = state["modal_message_id"]
        embed = await self._step_embed(thread.guild, state, step)
        can_acknowledge = step == 3 and await self._report_is_open(thread.guild.id, state)
        try:
            message = await thread.send(embed=embed, view=controls(checklist_controls(step, can_acknowledge=can_acknowledge)), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.warning("Could not post the checklist in thread %s: %s", thread.id, error)
            return False
        await self._update_state(thread.id, current_step=step, modal_message_id=message.id)
        if previous and previous != message.id:
            await self._delete_by_id(thread, previous)
        return True

    async def _delete_by_id(self, thread: Any, message_id: int) -> None:
        try:
            await thread.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass

    async def _close_review(self, thread: Any, state: dict) -> None:
        """Runs under the thread's lock. Closes for good and leaves nothing behind: the checklist is deleted and nothing replaces it."""
        message_id = state["modal_message_id"]
        await self._update_state(thread.id, status=DONE, current_step=len(REVIEW_STEPS) - 1, modal_message_id=None)
        if message_id:
            await self._delete_by_id(thread, message_id)

    # ---- review threads: clicks ----

    async def _active_review(self, interaction: discord.Interaction) -> tuple[Any, dict] | None:
        """The guard on every checklist button. Rejects staff-only clicks from others, leftover copies and closed reviews, and replies each time."""
        if not self.is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use this checklist.", ephemeral=True)
            return None
        thread = interaction.channel
        if not is_thread(thread):
            await interaction.response.send_message("This control only works inside a thread.", ephemeral=True)
            return None
        state = await self._state(thread.id)
        if state is not None:
            refusal = self._subject_refusal(interaction.user, state["user_id"])
            if refusal:
                await interaction.response.send_message(refusal, ephemeral=True)
                return None
        if state is None:
            await interaction.response.send_message("This review is already closed. Removing this leftover checklist.", ephemeral=True)
            await _delete_quietly(interaction.message)
            return None
        if state["status"] != ACTIVE:
            await interaction.response.send_message(f"This review is already marked {state['status']}. Removing this leftover checklist.", ephemeral=True)
            await _delete_quietly(interaction.message)
            return None
        tracked = state["modal_message_id"]
        if tracked and interaction.message is not None and interaction.message.id != tracked:
            await interaction.response.send_message("That was an older copy of the checklist, so it has been removed. Use the current one at the bottom of this thread.", ephemeral=True)
            await _delete_quietly(interaction.message)
            return None
        return thread, state

    async def press_start(self, interaction: discord.Interaction) -> None:
        resolved = await self._active_review(interaction)
        if resolved is None:
            return
        thread, state = resolved
        if state["current_step"] == 0:
            if thread.id in self._gathering:
                await interaction.response.send_message("Someone is already gathering for this review.", ephemeral=True)
                return
            await interaction.response.send_modal(GatherForm(self))
            return
        if state["current_step"] == 3 and await self._report_is_open(thread.guild.id, state):
            await interaction.response.defer()
            for report in await self._open_in_review(thread.guild.id, state):
                fresh = await self.change_state(report, ACTIONED, interaction.user)
                if fresh is not None:
                    await self.sync_report_post(fresh)
            await self._tell_if_refused(interaction, await self.show_step(thread, 4, expected=3))
            return
        await interaction.response.send_message("This step is completed or requires manual action.", ephemeral=True)

    async def press_completed(self, interaction: discord.Interaction) -> None:
        resolved = await self._active_review(interaction)
        if resolved is None:
            return
        thread, state = resolved
        await interaction.response.defer()
        await self._tell_if_refused(interaction, await self.show_step(thread, state["current_step"] + 1, expected=state["current_step"]))

    @staticmethod
    async def _tell_if_refused(interaction: discord.Interaction, posted: bool) -> None:
        if not posted:
            await interaction.followup.send("I couldn't post the next step, so the review stays where it was. Check that I can send messages in this thread and that it isn't locked.", ephemeral=True)

    async def press_dismiss(self, interaction: discord.Interaction) -> None:
        if not self.is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use this checklist.", ephemeral=True)
            return
        thread = interaction.channel
        known = await self._state(thread.id) if is_thread(thread) else None
        refusal = self._subject_refusal(interaction.user, known["user_id"]) if known else None
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.send_message("Dismissed. This checklist will not post again on its own.", ephemeral=True)
        tracked = None
        if is_thread(thread):
            async with self._thread_lock(thread.id):
                state = await self._state(thread.id)
                tracked = state["modal_message_id"] if state else None
                await self._update_state(thread.id, status=DISMISSED, modal_message_id=None)
        await _delete_quietly(interaction.message)
        # The click may have come from an older copy, so the current checklist goes as well and nothing stays behind.
        if tracked and (interaction.message is None or tracked != interaction.message.id):
            await self._delete_by_id(thread, tracked)

    async def submit_gather(self, interaction: discord.Interaction, raw_messages: str, raw_logs: str) -> None:
        thread = interaction.channel
        if not is_thread(thread):
            await interaction.response.send_message("This form only works inside a thread.", ephemeral=True)
            return
        try:
            message_count, log_count = int(raw_messages.strip()), int(raw_logs.strip())
        except ValueError:
            await interaction.response.send_message("Both boxes need a whole number.", ephemeral=True)
            return
        if not (1 <= message_count <= GATHER_MAX and 1 <= log_count <= GATHER_MAX):
            await interaction.response.send_message(f"Both numbers must be between 1 and {GATHER_MAX}.", ephemeral=True)
            return
        if not self.is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use this checklist.", ephemeral=True)
            return
        state = await self._state(thread.id)
        # The form stayed open for a while, so the review may have moved on since the button was pressed.
        if state is None or state["status"] != ACTIVE or state["current_step"] != 0:
            await interaction.response.send_message("This review has moved past gathering.", ephemeral=True)
            return
        refusal = self._subject_refusal(interaction.user, state["user_id"])
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if thread.id in self._gathering:
            await interaction.response.send_message("Someone is already gathering for this review.", ephemeral=True)
            return
        self._gathering.add(thread.id)  # before anything waits, so a second form landing in the gap is turned away above
        try:
            await interaction.response.defer()
            counts = await self.gather(thread, state["user_id"], message_count, log_count, viewer=interaction.user)
        finally:
            self._gathering.discard(thread.id)
        if counts["refused"] and not counts["posted"]:
            await interaction.followup.send("Discord refused every post, so this stays on step one. Check that I can send messages and embeds in this thread.", ephemeral=True)
            return
        await interaction.followup.send(self._gather_summary(counts), ephemeral=True)
        await self._tell_if_refused(interaction, await self.show_step(thread, 1, expected=0))

    @staticmethod
    def _gather_summary(counts: dict) -> str:
        if not counts["messages"] and not counts["logs"]:
            answer = "Found no messages or log entries for this member."
        else:
            answer = f"Posted {_many(counts['messages'], 'message')} and {'1 log entry' if counts['logs'] == 1 else str(counts['logs']) + ' log entries'}."
        if counts["refused"]:
            answer += f" Discord refused {'1 entry' if counts['refused'] == 1 else str(counts['refused']) + ' entries'}."
        if counts["left_out"]:
            answer += f" Left out {_many(counts['left_out'], 'item')} from channels you or the moderator role can't read."
        return answer

    # ---- review threads: gathering ----

    def _gather_embed(self, guild: Any, row: dict, user_id: int, user_name: str) -> discord.Embed:
        member = guild.get_member(user_id)
        created = row.get("created_at")
        embed = discord.Embed(description=_fit(row.get("content") or "", 4096) or None, timestamp=datetime.fromtimestamp(created, timezone.utc) if created else None)
        embed.set_author(name=member.display_name if member else user_name, icon_url=member.display_avatar.url if member else None)
        url = _message_url(guild.id, row["channel_id"], row["id"])
        embed.add_field(name="Channel", value=f"<#{row['channel_id']}> [Jump to message]({url})", inline=True)
        if row.get("deleted_at"):
            embed.add_field(name="Deleted", value=_short_stamp(row["deleted_at"]), inline=True)
        image_set = False
        for attachment in row.get("attachments") or []:
            if (attachment.get("content_type") or "").startswith("image") and not image_set:
                embed.set_image(url=unsigned(attachment.get("url")))  # a stored link's signature runs out, and Discord renews one sent to it bare
                image_set = True
            elif len(embed.fields) < 24:
                embed.add_field(name="Attachment", value=_fit(attachment.get("url") or attachment.get("filename") or "file", 1024), inline=False)
        return embed

    async def gather(self, thread: Any, user_id: int, message_count: int, log_count: int, *, viewer: Any) -> dict:
        """Post a member's recent messages, threads and log entries into their review thread, from what `viewer` and the moderator role can read. Returns what it did.

        The message store answers without touching Discord, so this is normally instant. Only what it doesn't hold falls back to reading
        channels, which on a large server takes minutes and shows a progress message, always removed afterward.
        """
        guild = thread.guild
        logging_module = self.bot.module("logging")
        stored = getattr(logging_module, "user_messages", None)
        found = list(await stored(guild.id, user_id, limit=message_count)) if stored else []
        rows = [r for r in found if self._may_gather_from(guild, viewer, r["channel_id"])]
        if stored and len(rows) < len(found):  # a long run in hidden channels shouldn't send this off to read every channel
            found = list(await stored(guild.id, user_id, limit=min(message_count * 4, 5000)))
            rows = [r for r in found if self._may_gather_from(guild, viewer, r["channel_id"])]
        left_out = len(found) - len(rows)
        rows = rows[:message_count]
        if len(rows) < message_count:
            oldest = min((r["id"] for r in found), default=None)
            rows += await self._scan_channels(thread, guild, user_id, message_count - len(rows), oldest, viewer)
        refused = await self._post_embeds(thread, [self._gather_embed(guild, row, user_id, name_in_thread(thread.name)) for row in rows])
        message_total = len(rows)

        owned_all = [t for t in guild.threads if t.owner_id == user_id]
        owned = [t for t in owned_all if self._may_gather_from(guild, viewer, t.id)][:message_count]
        left_out += len([t for t in owned_all if not self._may_gather_from(guild, viewer, t.id)])
        if owned:
            await self._post_lines(thread, [f"**Thread:** {discord.utils.escape_markdown(t.name)}\n[Link to thread]({t.jump_url})" for t in owned])

        reader = getattr(logging_module, "user_log_entries", None)
        entries = list(await reader(guild.id, user_id, limit=log_count, kind="message")) if reader else []
        allowed = [e for e in entries if not e.get("channel_id") or self._may_gather_from(guild, viewer, e["channel_id"])]
        left_out += len(entries) - len(allowed)
        embeds = [discord.Embed.from_dict(e["embed"]) for e in allowed[:log_count] if isinstance(e.get("embed"), dict)]
        if len(embeds) < log_count:
            # What the store gave was also posted to that channel, so only what's older than all of it is new.
            embeds += await self._scan_log_channel(guild, user_id, log_count - len(embeds), min((e["at"] for e in entries), default=None), viewer)
        refused += await self._post_embeds(thread, embeds)
        posted = message_total + len(embeds) - refused
        if left_out:
            await self._post(thread, content=f"Left out {_many(left_out, 'item')} from channels the person gathering or the moderator role can't read.")
        if refused and posted:
            await self._post(thread, content=f"{'1 entry' if refused == 1 else str(refused) + ' entries'} couldn't be posted.")
        await self._refresh_stats(thread, guild, user_id, name_in_thread(thread.name))
        return {"messages": message_total, "logs": len(embeds), "refused": refused, "posted": posted, "left_out": left_out}

    async def _post_lines(self, thread: Any, lines: list[str]) -> None:
        for piece in in_pieces(lines, joiner="\n\n"):
            await self._post(thread, content=piece)
            await asyncio.sleep(POST_PAUSE)

    async def _scan_channels(self, thread: Any, guild: Any, user_id: int, wanted: int, older_than: int | None, viewer: Any) -> list[dict]:
        """The older way, for what the store doesn't hold: read recent history in every channel the bot can read and the person asking may gather from."""
        me = guild.me
        channels = [c for c in guild.text_channels if c.permissions_for(me).read_message_history and self._may_gather_from(guild, viewer, c.id)]
        channels += [t for t in guild.threads if t.id != thread.id and t.permissions_for(me).read_message_history and self._may_gather_from(guild, viewer, t.id)]
        if not channels:
            return []
        progress = await self._post(thread, content="Gathering messages: 0% complete")
        started = time.time()
        found: list[Any] = []
        try:
            for start in range(0, len(channels), SCAN_BATCH):
                batch = channels[start:start + SCAN_BATCH]
                for messages in await asyncio.gather(*(self._read_channel(c, user_id, older_than) for c in batch)):
                    found.extend(messages)
                done = min(start + SCAN_BATCH, len(channels))
                if progress is not None:
                    left = (time.time() - started) / done * (len(channels) - done)
                    try:
                        await progress.edit(content=f"Gathering messages: {int(done / len(channels) * 100)}% complete, done <t:{int(time.time() + left)}:R>")
                    except discord.HTTPException:
                        pass
                if done < len(channels):
                    await asyncio.sleep(SCAN_PAUSE)
        finally:
            await _delete_quietly(progress)
        found.sort(key=lambda m: m.created_at, reverse=True)
        return [_stored_form(m) for m in found[:wanted]]

    async def _read_channel(self, channel: Any, user_id: int, older_than: int | None) -> list[Any]:
        kept = []
        try:
            async for message in channel.history(limit=SCAN_PER_CHANNEL, before=discord.Object(older_than) if older_than else None):
                if message.author.id == user_id:
                    kept.append(message)
        except discord.HTTPException as error:
            log.warning("Could not read channel %s: %s", channel.id, error)
        return kept

    async def _scan_log_channel(self, guild: Any, user_id: int, wanted: int, older_than: float | None, viewer: Any) -> list[discord.Embed]:
        """Log entries from before the store existed live only in the channel they were posted to."""
        channel_id = self.cfg(guild.id)["log_channel_fallback"]
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        if channel is None or not hasattr(channel, "history") or not self._may_gather_from(guild, viewer, channel.id):
            return []
        embeds: list[discord.Embed] = []
        before = datetime.fromtimestamp(older_than, timezone.utc) if older_than else None
        try:
            async for message in channel.history(limit=1000, before=before):
                if len(embeds) >= wanted:
                    break
                if not message.embeds:
                    continue
                footer = message.embeds[0].footer.text if message.embeds[0].footer else ""
                match = re.search(r"ID:\s*(\d+)", footer or "")
                if match and int(match.group(1)) == user_id:
                    embeds.append(message.embeds[0])
        except discord.HTTPException as error:
            log.warning("Could not read log channel %s: %s", channel_id, error)
        return embeds

    # ---- review threads: keeping them right ----

    async def _report_posts_gone(self, guild_id: int | None, message_ids: Any) -> None:
        """A report whose post is deleted stays on record with no link to a post that's gone. The post ID is indexed, so this stays cheap on every delete."""
        if not guild_id:
            return
        ids = list(message_ids)
        await self.db.run(f"UPDATE reports SET post_message_id = NULL WHERE guild_id = ? AND post_message_id IN ({', '.join('?' * len(ids))})", (guild_id, *ids))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: Any) -> None:
        await self._report_posts_gone(payload.guild_id, [payload.message_id])
        if not payload.guild_id or not self.cfg(payload.guild_id)["forum_channel"]:
            return
        state = await self._state(payload.channel_id)
        if state is not None and state["status"] == ACTIVE and state["modal_message_id"] == payload.message_id:
            self._flagged.add(payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: Any) -> None:
        await self._report_posts_gone(payload.guild_id, payload.message_ids)
        if not payload.guild_id or not self.cfg(payload.guild_id)["forum_channel"]:
            return
        state = await self._state(payload.channel_id)
        if state is not None and state["status"] == ACTIVE and state["modal_message_id"] in payload.message_ids:
            self._flagged.add(payload.channel_id)

    @tasks.loop(seconds=RECONCILE_SECONDS)
    async def _reconcile_loop(self) -> None:
        """A deleted checklist is seen as it happens, so most ticks have one thread at most to look at. A pass over every running review now and then catches anything missed."""
        self._ticks += 1
        try:
            if self._ticks % FULL_PASS_EVERY == 0:
                await self.reconcile()
            elif self._flagged:
                flagged, self._flagged = self._flagged, set()
                await self.reconcile(flagged)
        except Exception:
            log.exception("Checklist pass failed")  # an uncaught error would stop this loop for good

    async def reconcile(self, thread_ids: Any = None) -> None:
        """Repost a checklist only if the message its thread owns has really been deleted.

        A pass over every running review looks only at threads Discord keeps in memory for us, which are the active ones. An unfinished
        review's thread archives itself sooner or later and would only be left alone anyway, and reviews like that pile up over a year.
        """
        every_review = thread_ids is None
        if every_review:
            rows = await self.db.all("SELECT thread_id FROM review_state WHERE status = 'active'")
            thread_ids = [row["thread_id"] for row in rows]
        for thread_id in thread_ids:
            if thread_id in self._gathering:
                continue
            try:
                await self._reconcile_one(thread_id, look_up=not every_review)
            except discord.HTTPException as error:
                log.warning("Could not reconcile thread %s: %s", thread_id, error)

    async def _reconcile_one(self, thread_id: int, *, look_up: bool) -> None:
        state = await self._state(thread_id)
        if state is None or state["status"] != ACTIVE:
            return
        guild = self.bot.get_guild(state["guild_id"])
        if guild is None:
            return
        thread = guild.get_thread(thread_id)
        if thread is None:
            if not look_up:
                return
            try:
                thread = await guild.fetch_channel(thread_id)
            except discord.NotFound:
                await self.db.run("DELETE FROM review_state WHERE thread_id = ?", (thread_id,))
                return
        if not is_thread(thread) or thread.archived:
            return  # posting in an archived thread would unarchive it and bump the forum
        async with self._thread_lock(thread_id):
            # Everything above took network time, so this reads again before acting, and a review finished meanwhile stays finished.
            fresh = await self._state(thread_id)
            if fresh is None or fresh["status"] != ACTIVE:
                return
            message, known = await self._fetch_tracked(thread, fresh)
            if not known or message is not None:
                return
            if not fresh["modal_message_id"]:  # a review carried over from an earlier bot, posting here for the first time
                await self._retire_foreign_checklists(thread)
            await self._post_checklist(thread, fresh, fresh["current_step"])

    async def sweep_checklists(self) -> None:
        """Clear out extra checklist copies, which a crash between posting and deleting can leave.

        A running review keeps its newest checklist and loses the rest. A closed review loses all of them, so no dead buttons remain. The
        survivor is corrected in place when it shows the wrong step, which updates it without bumping the thread. Archived threads are
        skipped, because writing to one would unarchive it and bump the forum.
        """
        rows = await self.db.all("SELECT thread_id, guild_id, status FROM review_state")
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            thread = guild.get_thread(row["thread_id"]) if guild else None
            if thread is None or thread.archived or row["thread_id"] in self._gathering:
                continue
            try:
                await self._sweep_thread(thread, row)
            except discord.HTTPException as error:
                log.warning("Could not sweep thread %s: %s", thread.id, error)

    async def _sweep_thread(self, thread: Any, row: dict) -> None:
        me = thread.guild.me.id
        copies = []
        async for message in thread.history(limit=200):
            if message.author.id == me and message.embeds and STEP_TITLE.match(message.embeds[0].title or ""):
                copies.append(message)
        if not copies:
            return
        copies.sort(key=lambda m: m.id)
        async with self._thread_lock(thread.id):
            state = await self._state(thread.id)
            if state is None:
                return
            survivor = copies[-1] if state["status"] == ACTIVE else None
            for stale in copies:
                if stale is not survivor:
                    await _delete_quietly(stale)
                    await asyncio.sleep(DELETE_PAUSE)
            await self._update_state(thread.id, modal_message_id=survivor.id if survivor else None)
            if survivor is None or survivor.embeds[0].title == f"Step {state['current_step'] + 1}":
                return
            embed = await self._step_embed(thread.guild, state, state["current_step"])
            can_acknowledge = state["current_step"] == 3 and await self._report_is_open(thread.guild.id, state)
            await survivor.edit(embed=embed, view=controls(checklist_controls(state["current_step"], can_acknowledge=can_acknowledge)))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._started:
            return
        self._started = True
        self._spawn(self._startup())

    async def _startup(self) -> None:
        for outcome in await asyncio.gather(*(self.load_thread_index(guild) for guild in self.bot.guilds), return_exceptions=True):
            if isinstance(outcome, Exception):
                log.error("Could not read a review forum at startup", exc_info=outcome)
        await self.sweep_checklists()
        await self.reconcile()
        if not self._reconcile_loop.is_running():
            self._reconcile_loop.start()

    # ---- moderator pings ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        cfg = self.cfg(message.guild.id)
        word = cfg["mod_ping_text"].lower()
        role_id = cfg["moderator_role"]
        typed = bool(word) and word in (message.content or "").lower()
        pinged = bool(role_id) and any(role.id == role_id for role in message.role_mentions)
        if not (typed or pinged):
            return
        permissions = getattr(message.author, "guild_permissions", None)
        if permissions is None or permissions.manage_guild or permissions.administrator:
            return
        await self.db.run(
            "INSERT INTO moderator_pings (guild_id, user_id, count, last_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + 1, last_at = excluded.last_at",
            (message.guild.id, message.author.id, time.time()),
        )

    # ---- moved tools ----

    @commands.hybrid_command(name="punishment_notice", description="Write the standard moderation notice for a ticket with a form")
    @commands.guild_only()
    @app_commands.describe(user="The member the notice is addressed to")
    @staff_only("Staff")
    async def punishment_notice(self, ctx: commands.Context, user: discord.Member) -> None:
        if not self.is_staff(ctx.author):
            await ctx.fail("Only staff can write a punishment notice.")
            return
        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(NoticeForm(self, user))
            return
        await ctx.send(f"Press the button to write the notice for {user.mention}.", view=OpenNoticeView(self, ctx.author.id, user), delete_after=300, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="delete_messages", description="Delete messages in a channel or thread by filters")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.default_permissions(administrator=True)
    async def delete_messages(self, ctx: commands.Context, *, flags: DeleteFlags) -> None:
        channel = flags.channel or ctx.channel
        if not hasattr(channel, "delete_messages") or not hasattr(channel, "history"):
            await ctx.fail("Pick a text channel or a thread.")
            return
        if not channel.permissions_for(ctx.author).view_channel:
            await ctx.fail("You can't see that channel.")
            return
        if not channel.permissions_for(ctx.guild.me).manage_messages:
            await ctx.fail(f"I need Manage Messages in {channel.mention}.")
            return
        # With no filter this would empty the whole channel, and one slip of the Enter key shouldn't be able to do that.
        if not (flags.user or flags.count or flags.until_date or flags.keyword or flags.gifs_only):
            await ctx.fail("Pick at least one filter: user, count, until_date, keyword or gifs_only.")
            return
        target_id = None
        if flags.user:
            target_id = parse_user_id(flags.user)
            if target_id is None:
                await ctx.fail("The user must be a mention or a numeric ID.")
                return
        until = None
        if flags.until_date:
            try:
                until = datetime.strptime(flags.until_date.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                await ctx.fail("The date must look like 2026-11-05.")
                return
        if flags.count is not None and flags.count < 1:
            await ctx.fail("The count must be at least 1.")
            return
        job = self._lock(("delete", channel.id))
        if job.locked():
            await ctx.fail(f"A delete is already running in {channel.mention}.")
            return
        await ctx.defer(ephemeral=True)
        pattern = None
        if flags.keyword and flags.keyword.startswith("re:"):
            pattern = flags.keyword[3:]
            problem = await self.regex.problem(pattern)
            if problem:
                await ctx.fail(problem)
                return
        progress = await ctx.send("Looking through messages...", ephemeral=True)
        async with job:
            processed, deleted, problem = await self._run_delete(progress, ctx, channel, target_id, flags, until, pattern)
            await self.regex.close()  # this command is rare, so its worker process doesn't stay around between runs
        ending = f"Finished. Looked through {_many(processed, 'message')} and deleted {deleted}." if not problem else f"Stopped after looking through {_many(processed, 'message')} and deleting {deleted}. {problem}"
        # One message tells how it went, so nothing saying "Looking through messages" stays behind. A slash command's token lasts 15 minutes,
        # and a job can outlast it, so a failed edit falls back to a new message. A text command's note sits in the channel and comes down.
        if ctx.interaction is not None and await self._note_progress(progress, ending):
            return
        await _delete_quietly(progress)
        await ctx.send(ending, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _run_delete(self, progress: Any, ctx: commands.Context, channel: Any, target_id: int | None, flags: DeleteFlags, until: datetime | None, pattern: str | None) -> tuple[int, int, str | None]:
        processed = deleted = 0
        waiting: list[Any] = []
        last_note = time.monotonic()
        reason = f"Delete command by {ctx.author}"

        async def settle(messages: list[Any]) -> int:
            """Match a page of messages and delete the matches. Returns how many went."""
            kept = []
            for message in messages:
                if not self._passes(message, target_id, flags, pattern):
                    continue
                if pattern is None or await self._pattern_matches(pattern, message):
                    kept.append(message)
            return await self._delete_batch(channel, kept, reason)

        problem = None
        too_slow = "That regex took too long to run, so nothing more was deleted."
        try:
            # A typed command and its progress note sit in the channel, and neither is one of the messages it was asked to look through.
            before = ctx.message if ctx.interaction is None and channel.id == ctx.channel.id else None
            async for message in channel.history(limit=flags.count, before=before, after=until, oldest_first=False):
                if until and message.created_at < until:
                    break
                processed += 1
                waiting.append(message)
                if len(waiting) >= 100:
                    deleted += await settle(waiting)
                    waiting = []
                    if pattern and self.regex.took_too_long(pattern):
                        problem = too_slow
                        break
                if time.monotonic() - last_note >= PROGRESS_SECONDS:
                    last_note = time.monotonic()
                    await self._note_progress(progress, f"Looked through {_many(processed, 'message')}. Deleted {deleted} so far.")
            if waiting and problem is None:
                deleted += await settle(waiting)
                if pattern and self.regex.took_too_long(pattern):
                    problem = too_slow
        except discord.Forbidden:
            problem = "Discord refused, so check my permissions in that channel."
        except discord.HTTPException as error:
            problem = f"Discord refused: {error.text or error.status}"
        return processed, deleted, problem

    async def _pattern_matches(self, pattern: str, message: Any) -> bool:
        for part in message_texts(message):
            if part and await self.regex.search(pattern, part):
                return True
        return False

    @staticmethod
    def _passes(message: Any, target_id: int | None, flags: DeleteFlags, pattern: str | None) -> bool:
        if target_id and message.author.id != target_id:
            return False
        if flags.gifs_only and not has_gif(message):
            return False
        if flags.keyword and not pattern and not any(flags.keyword in part for part in message_texts(message)):
            return False
        return True

    @staticmethod
    async def _note_progress(progress: Any, content: str) -> bool:
        if progress is None:
            return False
        try:
            await progress.edit(content=content)
            return True
        except discord.HTTPException:
            return False  # a job can outlast the slash command's token, and the deleting still matters more

    @staticmethod
    async def _delete_batch(channel: Any, messages: list[Any], reason: str) -> int:
        """Discord deletes recent messages in bulk and older ones one at a time."""
        cutoff = discord.utils.utcnow() - BULK_DELETE_AGE
        recent = [m for m in messages if m.created_at > cutoff]
        old = [m for m in messages if m.created_at <= cutoff]
        count = 0
        for start in range(0, len(recent), 100):
            chunk = recent[start:start + 100]
            await channel.delete_messages(chunk, reason=reason)
            count += len(chunk)
        for message in old:
            try:
                await message.delete()
                count += 1
            except discord.NotFound:
                pass
        return count

    @commands.hybrid_command(name="role_order", description="List the server's roles in display order, top to bottom")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.default_permissions(administrator=True)
    async def role_order(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        roles = sorted(await ctx.guild.fetch_roles(), key=lambda r: r.position, reverse=True)
        chunk = ""
        for role in roles:
            line = f"{role.position:>3}  {role.name.replace('`', chr(39))}\n"
            if len(chunk) + len(line) > 1900:
                await ctx.send(f"```\n{chunk}```", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                chunk = ""
            chunk += line
        if chunk:
            await ctx.send(f"```\n{chunk}```", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(name="fun_roles", fallback="list", description="Show the server's fun role pack")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.default_permissions(administrator=True)
    async def fun_roles(self, ctx: commands.Context) -> None:
        pack = self.cfg(ctx.guild.id)["fun_roles"]
        if not pack:
            await ctx.fail("The fun role pack is empty. Add roles to it on the Reports page.")
            return
        lines = [f"{row['emoji']} `{row['name']}`" for row in sorted(pack, key=lambda r: r["name"].casefold())]
        for piece in in_pieces(lines):
            await ctx.send(piece, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="deploy_fun_roles", description="Create the role pack as an alphabetical block directly below a role")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(anchor="The role the block goes directly below", style="Where each emoji goes", dry_run="Preview the plan without changing anything")
    async def deploy_fun_roles(self, ctx: commands.Context, anchor: discord.Role, style: Literal["icon", "prefix", "both"] = "icon", dry_run: bool = True) -> None:
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        pack = self.cfg(guild.id)["fun_roles"]
        if not pack:
            await ctx.fail("The fun role pack is empty. Add roles to it on the Reports page.")
            return
        if anchor.is_default():
            await ctx.fail("The anchor can't be @everyone. Pick the role the block should sit directly below.")
            return
        if not guild.me.guild_permissions.manage_roles:
            await ctx.fail("I need the Manage Roles permission to do this.")
            return
        if anchor >= guild.me.top_role:
            await ctx.fail(
                f"My highest role ({guild.me.top_role.name}, position {guild.me.top_role.position}) must be above the anchor "
                f"({anchor.name}, position {anchor.position}). Move my role higher or pick a lower anchor."
            )
            return
        use_icon = style in ("icon", "both")
        use_prefix = style in ("prefix", "both")
        if use_icon and "ROLE_ICONS" not in guild.features:
            await ctx.fail("This server has no role icons, which need boost level 2. Run it again with style: prefix.")
            return
        plan = [(base, emoji, name, find_pack_role(guild.roles, base, emoji)) for base, emoji, name in pack_entries(pack, prefix=use_prefix)]
        if dry_run:
            await self._send_plan(ctx, anchor, plan)
            return
        reason = f"Fun role pack by {ctx.author}"
        created, failures = [], []
        for base, emoji, name, existing in plan:
            if existing is not None:
                continue
            kwargs: dict[str, Any] = {"name": name, "reason": reason}
            if use_icon and emoji:
                kwargs["display_icon"] = emoji
            try:
                await guild.create_role(**kwargs)
                created.append(base)
            except discord.HTTPException as error:
                failures.append(f"{base}: {error.text or error.status}")
            await asyncio.sleep(ROLE_PAUSE)
        if failures:
            await ctx.fail(f"Created {_many(len(created), 'role')}, then stopped: some creations failed and nothing was moved. Fix the cause and run it again. Existing roles get reused.\n" + _fit("\n".join(failures), 1500))
            return
        await self._position_block(ctx, anchor, plan, reason, len(created))

    async def _send_plan(self, ctx: commands.Context, anchor: Any, plan: list) -> None:
        missing = sum(1 for row in plan if row[3] is None)
        lines = [f"**Dry run.** {missing} to create, {len(plan) - missing} already exist. The block will sit directly below **{discord.utils.escape_markdown(anchor.name)}**, in this order, top to bottom:"]
        for base, emoji, name, existing in plan:
            lines.append(f"{emoji} `{name}` ({'exists, will be moved' if existing else 'create'})")
        lines.append("\nRun it again with `dry_run: False` to do it.")
        for piece in in_pieces(lines):
            await ctx.send(piece, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _position_block(self, ctx: commands.Context, anchor: Any, plan: list, reason: str, created: int) -> None:
        """Move the block directly below the anchor, following the pattern of discord.py's Role._move: renumber only roles whose position
        value falls in the bottom range up through the anchor, and never send entries for anything above it. Live servers carry sparse and
        duplicated position values, so compact indexes aren't valid outside this segment."""
        guild = ctx.guild
        fresh = await guild.fetch_roles()
        block = []
        for base, emoji, _, _ in plan:
            role = find_pack_role(fresh, base, emoji)
            if role is None:
                await ctx.fail(f"Lost track of the role {base} after creating it, so nothing was moved.")
                return
            block.append((role, base))
        block_ids = {role.id for role, _ in block}
        anchor_now = next((r for r in fresh if r.id == anchor.id), None)
        if anchor_now is None:
            await ctx.fail("The anchor role disappeared partway through, so nothing was moved.")
            return
        # Bottom-up order as discord.py sorts roles: position ascending, with ties broken by the newer ID sorting lower.
        canonical = sorted(fresh, key=lambda r: (r.position, -r.id))
        top_value = max([anchor_now.position] + [r.position for r, _ in block])
        change_range = range(1, top_value + 1)
        segment = [r for r in canonical if not r.is_default() and r.position in change_range]
        my_top = guild.me.top_role
        blocked = [r.name for r in segment if r.id != my_top.id and not (r < my_top)]
        if blocked:
            await ctx.fail("Nothing was moved. These roles sit inside the range to reorder but aren't below my top role: " + _fit(", ".join(blocked), 1500) + ". Run /role_order to see the list.")
            return
        if len(segment) > len(change_range):
            await ctx.fail(f"Nothing was moved. {len(segment)} roles are packed into position values 1 to {top_value}, so the block can't be renumbered without touching roles above the anchor. Run /role_order to see the list.")
            return
        rest = [r for r in segment if r.id not in block_ids]
        anchor_index = next((i for i, r in enumerate(rest) if r.id == anchor_now.id), None)
        if anchor_index is None:
            await ctx.fail("The anchor fell outside the range to reorder, so nothing was moved. Run /role_order to see the list.")
            return
        # A bottom-up splice, so Z goes in first and A ends up at the top of the block.
        bottom_up = [r for r, _ in sorted(block, key=lambda p: p[1].casefold(), reverse=True)]
        desired = rest[:anchor_index] + bottom_up + rest[anchor_index:]
        positions = {role: value for role, value in zip(desired, change_range) if role.position != value}
        if positions:
            await guild.edit_role_positions(positions=positions, reason=reason)
        await ctx.ok(f"Created {_many(created, 'role')}, reused {len(plan) - created}, moved {len(positions)}. The block sits in alphabetical order directly below {anchor_now.name}. All other roles kept their order.")

    @commands.hybrid_command(name="delete_fun_roles", description="Delete every role from the fun role pack")
    @commands.guild_only()
    @requires("administrator")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(dry_run="Preview which roles would be deleted")
    async def delete_fun_roles(self, ctx: commands.Context, dry_run: bool = True) -> None:
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        targets = []
        for row in self.cfg(guild.id)["fun_roles"]:
            role = find_pack_role(guild.roles, row["name"], row.get("emoji") or "")
            if role is not None:
                targets.append(role)
        if not targets:
            await ctx.fail("No fun pack roles were found on this server.")
            return
        if dry_run:
            lines = [f"**Dry run.** {_many(len(targets), 'role')} would be deleted:"] + [f"`{r.name}` (position {r.position})" for r in targets] + ["\nRun it again with `dry_run: False` to do it."]
            for piece in in_pieces(lines):
                await ctx.send(piece, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        deleted, failures = [], []
        reason = f"Fun role pack removed by {ctx.author}"
        for role in targets:
            if role.managed or not (role < guild.me.top_role):
                failures.append(f"{role.name}: skipped, since it's managed or not below my top role")
                continue
            try:
                await role.delete(reason=reason)
                deleted.append(role.name)
            except discord.HTTPException as error:
                failures.append(f"{role.name}: {error.text or error.status}")
            await asyncio.sleep(ROLE_PAUSE)
        answer = f"Deleted {_many(len(deleted), 'role')}."
        if failures:
            answer += "\nFailed:\n" + "\n".join(failures)
        await ctx.ok(_fit(answer, 1900))

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.get("/reports", self.api_reports, access="staff")
        router.get("/reports/threads", self.api_threads, access="staff")
        router.get("/reports/pings", self.api_pings, access="staff")
        router.post("/reports/review", self.api_open_review, access="staff")
        router.get(r"/reports/{report_id:\d+}", self.api_report, access="staff")
        router.post(r"/reports/{report_id:\d+}/state", self.api_set_state, access="staff")
        router.post(r"/reports/{report_id:\d+}/case", self.api_set_case, access="staff")
        router.delete(r"/reports/{report_id:\d+}", self.api_delete_report)

    @staticmethod
    def _page_args(req: Any) -> tuple[int, int]:
        page = page_number(req.query.get("page", "1"), 0, 2 ** 31)
        size = page_number(req.query.get("size", "10"), 0, PAGE_MAX)
        if not page or not size:
            raise ApiError(400, "Page and size must be whole numbers.")
        return page, size

    @staticmethod
    def _person_filter(column_id: str, column_name: str, value: str, where: list[str], args: list[Any]) -> None:
        """A filter box takes an ID or part of a name."""
        cleaned = value.strip()
        if not cleaned:
            return
        person_id = whole_number(cleaned)
        if person_id is not None:
            where.append(f"{column_id} = ?")
            args.append(person_id)
        else:
            escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append(f"{column_name} LIKE ? ESCAPE '\\'")
            args.append(f"%{escaped}%")

    @staticmethod
    def _reporter_filter(value: str, where: list[str], args: list[Any]) -> None:
        """Whoever filed it, or anyone who reported the same thing afterward."""
        cleaned = value.strip()
        if not cleaned:
            return
        person_id = whole_number(cleaned)
        if person_id is not None:
            where.append("(reporter_id = ? OR EXISTS (SELECT 1 FROM json_each(also_reported) WHERE json_extract(value, '$.id') = ?))")
            args.extend([person_id, person_id])
            return
        escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(reporter_name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM json_each(also_reported) WHERE json_extract(value, '$.name') LIKE ? ESCAPE '\\'))")
        args.extend([f"%{escaped}%", f"%{escaped}%"])

    def _report_out(self, row: dict, req: Any) -> dict:
        """A report for the page. What was said in a channel this person can't read stays out, as it does on the history button."""
        guild_id = req.guild_id
        out = {k: v for k, v in row.items() if k not in ("post_text",)}
        out["files"] = json.loads(row["files"] or "[]")
        out["also_reported"] = json.loads(row["also_reported"] or "[]")
        out["content_hidden"] = bool(row["channel_id"]) and not req.can_read(row["channel_id"])
        if out["content_hidden"]:
            out.update(content="", files=[])
        out["post_url"] = _message_url(guild_id, row["post_channel_id"], row["post_message_id"]) if row["post_message_id"] else None
        out["thread_url"] = f"https://discord.com/channels/{guild_id}/{row['thread_id']}" if row["thread_id"] else None
        return out

    async def api_reports(self, req: Any) -> dict:
        page, size = self._page_args(req)
        where = ["guild_id = ?"]
        args: list[Any] = [req.guild_id]
        state = req.query.get("state", "")
        if state:
            if state not in STATES:
                raise ApiError(400, "That isn't a report state.")
            where.append("state = ?")
            args.append(state)
        self._reporter_filter(req.query.get("reporter", ""), where, args)
        self._person_filter("target_id", "target_name", req.query.get("target", ""), where, args)
        needle = req.query.get("q", "").strip()
        if needle:
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            # Searching what someone can't read would tell them what it says, one guess at a time.
            hidden = [row["channel_id"] for row in await self.db.all("SELECT DISTINCT channel_id FROM reports WHERE guild_id = ? AND channel_id IS NOT NULL", (req.guild_id,)) if not req.can_read(row["channel_id"])]
            leave_out = f" AND (channel_id IS NULL OR channel_id NOT IN ({', '.join('?' * len(hidden))}))" if hidden else ""
            where.append(f"(reason LIKE ? ESCAPE '\\' OR (content LIKE ? ESCAPE '\\'{leave_out}))")
            args.extend([f"%{escaped}%", f"%{escaped}%", *hidden])
        clause = " AND ".join(where)
        total = await self.db.scalar(f"SELECT COUNT(*) FROM reports WHERE {clause}", args)
        rows = await self.db.all(f"SELECT * FROM reports WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, size, (page - 1) * size])
        counts = {row["state"]: row["amount"] for row in await self.db.all("SELECT state, COUNT(*) AS amount FROM reports WHERE guild_id = ? GROUP BY state", (req.guild_id,))}
        return {"rows": [self._report_out(row, req) for row in rows], "total": total, "page": page, "size": size, "counts": {name: counts.get(name, 0) for name in STATES}}

    async def _report_or_404(self, req: Any) -> dict:
        row = await self.report_by_id(req.guild_id, req.int_param("report_id"))
        if row is None:
            raise ApiError(404, "That report doesn't exist.")
        return row

    async def api_report(self, req: Any) -> dict:
        row = await self._report_or_404(req)
        record = await self._member_record(req.guild_id, row["target_id"])
        cases = [{"number": c.get("number"), "action": c.get("action"), "reason": c.get("reason")} for c in record["cases"][:50]]
        return {**self._report_out(row, req), "cases": cases, "has_moderation": getattr(self.bot.module("moderation"), "history", None) is not None}

    def _refuse_the_subject(self, req: Any, subject_id: int) -> None:
        if req.user.id == subject_id and not req.is_admin:
            raise ApiError(403, SELF_REFUSAL)

    async def api_delete_report(self, req: Any) -> dict:
        row = await self._report_or_404(req)
        self._refuse_the_subject(req, row["target_id"])
        left = None
        if row["post_message_id"]:
            try:
                await req.ops.delete_message(row["post_channel_id"], row["post_message_id"])
            except OpsError as error:  # a channel that's gone or a refused delete mustn't keep the record forever
                log.info("Could not delete the post of report %s: %s", row["id"], error)
                left = _message_url(req.guild_id, row["post_channel_id"], row["post_message_id"])
        await self.db.run("DELETE FROM reports WHERE id = ? AND guild_id = ?", (row["id"], req.guild_id))
        await self.db.run("UPDATE review_state SET report_id = NULL WHERE guild_id = ? AND report_id = ?", (req.guild_id, row["id"]))
        await req.record("report.delete", detail=f"Report {row['number']}")
        return {"ok": True, "left": left}

    async def api_set_state(self, req: Any) -> dict:
        row = await self._report_or_404(req)
        form = await req.json()
        state = form.get("state")
        if not isinstance(state, str) or state not in STATES:
            raise ApiError(400, "That isn't a report state.")
        self._refuse_the_subject(req, row["target_id"])
        if state == OPEN and req.access != "full":
            raise ApiError(403, "You need Manage Server to reset a report.")
        fresh = await self.change_state(row, state, req.actor)
        if fresh is None:
            raise ApiError(409, "This report is already in that state or someone else just handled it.")
        await self.sync_report_post(fresh)
        await req.record("report.state", detail=f"Report {fresh['number']}: {state}")
        return self._report_out(fresh, req)

    async def api_set_case(self, req: Any) -> dict:
        row = await self._report_or_404(req)
        self._refuse_the_subject(req, row["target_id"])
        form = await req.json()
        raw = form.get("case")
        number = integer(1)(raw) if raw not in (None, "") else None
        if number is not None and getattr(self.bot.module("moderation"), "history", None) is not None:
            record = await self._member_record(req.guild_id, row["target_id"])
            if not any(c.get("number") == number for c in record["cases"]):
                raise ApiError(400, f"This member has no case {number}.", {"case": "This member has no such case."})
        await self.db.run("UPDATE reports SET case_number = ? WHERE id = ? AND guild_id = ?", (number, row["id"], req.guild_id))
        fresh = await self.report_by_id(req.guild_id, row["id"])
        await self.sync_report_post(fresh)
        await req.record("report.case", detail=f"Report {fresh['number']}: case {number or 'removed'}")
        return self._report_out(fresh, req)

    async def api_open_review(self, req: Any) -> dict:
        form = await req.json()
        user_id = snowflake(form.get("user_id"))
        self._refuse_the_subject(req, user_id)
        report_id = form.get("report_id")
        if report_id is not None:
            report = await self.report_by_id(req.guild_id, integer(1)(report_id))
            if report is None or report["target_id"] != user_id:
                raise ApiError(404, "That report doesn't exist.")
            if report["channel_id"] and not req.can_read(report["channel_id"]):  # the review thread gets a copy of what was said, and other staff read it
                raise ApiError(403, "You can't read the messages in that channel.")
            report_id = report["id"]
        result = await req.ops.open_review(req.guild_id, user_id, report_id)
        await req.record("review.open", detail=str(user_id))
        return result

    async def api_threads(self, req: Any) -> dict:
        page, size = self._page_args(req)
        where = ["t.guild_id = ?"]
        args: list[Any] = [req.guild_id]
        self._person_filter("t.user_id", "t.user_name", req.query.get("member", ""), where, args)
        status = req.query.get("status", "")
        if status == "none":
            where.append("s.status IS NULL")
        elif status:
            if status not in (ACTIVE, DONE, DISMISSED):
                raise ApiError(400, "That isn't a review status.")
            where.append("s.status = ?")
            args.append(status)
        clause = " AND ".join(where)
        joins = "FROM review_threads t LEFT JOIN review_state s ON s.thread_id = t.thread_id LEFT JOIN moderator_pings p ON p.guild_id = t.guild_id AND p.user_id = t.user_id"
        total = await self.db.scalar(f"SELECT COUNT(*) {joins} WHERE {clause}", args)
        rows = await self.db.all(
            f"SELECT t.user_id, t.user_name, t.thread_id, s.status, s.current_step, s.report_id, s.updated_at, COALESCE(p.count, 0) AS pings {joins} "
            f"WHERE {clause} ORDER BY COALESCE(s.updated_at, 0) DESC, t.thread_id DESC LIMIT ? OFFSET ?",
            [*args, size, (page - 1) * size],
        )
        for row in rows:
            row["url"] = f"https://discord.com/channels/{req.guild_id}/{row['thread_id']}"
        return {"rows": rows, "total": total, "page": page, "size": size, "steps": len(REVIEW_STEPS)}

    async def api_pings(self, req: Any) -> list[dict]:
        rows = await self.db.all("SELECT user_id, count, last_at FROM moderator_pings WHERE guild_id = ? ORDER BY count DESC LIMIT 25", (req.guild_id,))
        for row in rows:
            member = req.ops.member(req.guild_id, row["user_id"])
            row["name"] = member["display_name"] if member else str(row["user_id"])
        return rows

    async def seed_demo(self, guild_id: int) -> None:
        """Sample rows for the demo dashboard, so the tables can be seen filled in. The demo has no gateway, so nothing here reaches Discord."""
        ops = self.bot.ops
        channels = {c["name"]: c["id"] for c in ops.describe_guild(guild_id)["channels"]}
        admin, member = ops.members[0], ops.members[1]
        ghost = member["id"] + 7000
        now = time.time()
        samples = [
            ("message", OPEN, member["name"], member["id"], "Keeps posting the same link in every channel.", "Free printer parts, click here", 1),
            ("user", OPEN, "former_member", ghost, "Their profile picture breaks the rules.", "", 0),
            ("message", ACTIONED, member["name"], member["id"], "Insulting people in the help channel.", "That is the worst advice I have read all week", 0),
            ("message", FALSE_POSITIVE, "former_member", ghost, "I just don't like this.", "I think the other brand is better", 0),
            (VOICE, OPEN, member["name"], member["id"], "Playing loud music into the channel.", "", 0),
        ]
        for number, (kind, state, name, target, reason, content, files) in enumerate(samples, 1):
            message_id = 900000000000200000 + number if kind == "message" else None
            channel_id = channels["general"] if message_id else channels["Lounge"] if kind == VOICE else None
            kept = [{"filename": "screenshot.png", "name": "evidence-1.png", "size": 48211, "content_type": "image/png", "copied": True}] if files else []
            post = await ops.send_message(channels["reports"], {"content": "", "embeds": [{"description": f"Sample report {number}", "color": STATE_COLORS[state]}], "buttons": []})
            await self.db.insert(
                "INSERT INTO reports (guild_id, number, kind, state, reporter_id, reporter_name, target_id, target_name, reason, channel_id, message_id, message_created_at, content, "
                "attachment_count, reaction_count, files, jump_url, post_channel_id, post_message_id, post_text, handled_by, handled_name, handled_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, number, kind, state, admin["id"], admin["name"], target, name, reason, channel_id, message_id, now - 7200 * number if message_id else None,
                 content, files, 0, json.dumps(kept), _message_url(guild_id, channels["general"], message_id or post["id"]), channels["reports"], post["id"], f"Sample report {number}",
                 None if state == OPEN else admin["id"], None if state == OPEN else admin["name"], None if state == OPEN else now - 600, now - 3600 * number),
            )
        await self._index_thread(guild_id, member["id"], 900000000000400001, member["name"])
        await self._begin_review(900000000000400001, guild_id, member["id"], 1)
        await self._index_thread(guild_id, ghost, 900000000000400002, "former_member")
        await self._begin_review(900000000000400002, guild_id, ghost, 4)
        await self._update_state(900000000000400002, status=DONE, current_step=4)
        await self.db.run("INSERT INTO moderator_pings (guild_id, user_id, count, last_at) VALUES (?, ?, 3, ?)", (guild_id, member["id"], now - 5000))


def _is_card_for(url: str | None, description: str | None, message_id: int) -> bool:
    """Whether a log card is about this message: an edit or delete card links to it, and other kinds name it."""
    return (url or "").endswith(f"/{message_id}") or f"Message ID: {message_id}" in (description or "")


def _utc_text(moment: float | None) -> str:
    if not moment:
        return "unknown"
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _stored_form(message: Any) -> dict:
    """A message read from Discord, in the shape the message store gives."""
    return {
        "id": message.id, "channel_id": message.channel.id, "author_id": message.author.id, "content": message.content, "created_at": message.created_at.timestamp(),
        "attachments": [{"filename": a.filename, "url": a.url, "size": a.size, "content_type": a.content_type} for a in message.attachments],
        "edited_at": message.edited_at.timestamp() if message.edited_at else None, "deleted_at": None,
    }
