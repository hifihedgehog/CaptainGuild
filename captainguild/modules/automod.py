"""Automatic moderation rules: spam limits, invites, links, bad words, honeypots, and what happens when one trips.

Every message runs through the rules in one order and stops at the first that acts. A rule in dry-run mode only logs what it would have
done, and the next rule still gets its turn. The bot manager role and members with Manage Server, Administrator, Ban Members, Kick Members or Manage Messages are always immune.
"""
from __future__ import annotations

import datetime
import io
import json
import logging
import re
import time
import unicodedata
from collections import deque
from re import _parser as regex_parser
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from ..core import messages
from ..core.durations import humanize, parse_duration
from ..core.module import Module, requires
from ..core.perms import MODULE as COMMANDS_MODULE
from ..core.perms import Who, evaluate
from ..core.tagscript import RegexGuard
from ..core.validate import (
    BAN, CHANNEL_MESSAGE, DELETE, DM_OFFENDER, DRAMA, KICK, MUTE, TEMPBAN, TEMPMUTE, TIMEOUT, WARN, Field, Invalid, Schema, ValidationError, boolean, choice, integer,
    optional, punishment, snowflake, snowflakes, text,
)
from ..web.api import ApiError
from ..core import access
from .moderation import SEND_ERRORS, THRESHOLD_SOURCE, ModerationError, Someone, name_of, send_private, split_quoted

log = logging.getLogger(__name__)

LIST_MAX = 5000
WORD_MAX = 200
PATTERN_MAX = 500
SAVED_TEXT_PIECE = 4000  # what the pattern worker reads at a time
SAVED_TEXT_STEP = 3800
RATE_COUNT_MAX = 100_000
RATE_SECONDS_MAX = 86400
CAPS_MIN_LENGTH = 6
# Punishing again this soon after a rule tripped for the same person repeats nothing useful. The message still goes.
REPEAT_SECONDS = 5.0
SPAM_NOTICE_SECONDS = 5
NOTICE_SECONDS = 15
INVITE_CACHE_SECONDS = 3600.0
INVITE_CACHE_MAX = 5000
WINDOW_SWEEP_SECONDS = 600.0
EDITS_REMEMBERED = 2000
# A flood of made-up invite codes must not spend the route's limit, which every server's invite scan shares. Nobody posts four invites honestly on a rule that bans them.
INVITE_LOOKUPS_PER_MESSAGE = 3
LOG_DETAIL_MAX = 300
LOG_FIELD_MAX = 1000
LOG_FILE_MAX = 512 * 1024
# Shorter than this and two people saying the same thing is only a conversation.
COPY_MIN_LENGTH = 8
# A word with no Latin letter that folds to a short English word is usually a real word in its own alphabet, as the Russian for mouth reads as pot. Longer spellings are rarely an accident.
WHOLE_SCRIPT_MIN_ENTRY = 5
WARN_STEPS_MAX = 10
SAFE_FILE_TYPES = frozenset("png jpg jpeg gif svg bmp tif tiff webp webm mp4 mov pdf txt mp3 flac wav ogg".split())

# Heaviest first. A rule takes one of these, and a server can't combine them.
HEAVY_ACTIONS = ((BAN, "ban"), (TEMPBAN, "tempban"), (KICK, "kick"), (TIMEOUT, "timeout"), (MUTE, "mute"), (TEMPMUTE, "mute"))
TIMED = TEMPMUTE | TEMPBAN | TIMEOUT
PUNISHMENT_WORDS = {
    "delete": DELETE, "warn": WARN, "tempmute": TEMPMUTE, "mute": MUTE, "kick": KICK, "tempban": TEMPBAN, "ban": BAN, "timeout": TIMEOUT,
    "defer": DRAMA, "drama": DRAMA, "message": CHANNEL_MESSAGE, "dm": DM_OFFENDER, "pm": DM_OFFENDER,
}
ACTION_NAMES = ((DELETE, "delete"), (WARN, "warn"), (TEMPMUTE, "tempmute"), (MUTE, "mute"), (TIMEOUT, "timeout"), (KICK, "kick"), (TEMPBAN, "tempban"), (BAN, "ban"), (DRAMA, "post to drama"), (CHANNEL_MESSAGE, "message in channel"), (DM_OFFENDER, "DM offender"))

RULE_LABELS = {
    "honeypot": "Honeypot", "invites": "Invite spam", "words": "Bad words", "mentions": "Mention spam", "links": "Link spam", "caps": "Caps", "attachments": "Attachment spam",
    "spam": "Message spam", "warns": "Warn threshold", "media": "Media only", "files": "Unsafe file", "copies": "Copied text", "names": "Bad name",
}
RULE_NOTICES = {
    "honeypot": "that channel is a trap for spam accounts.", "invites": "invites to other servers aren't allowed here.", "words": "that language isn't allowed here.",
    "mentions": "that's too many mentions.", "links": "that link isn't allowed here.", "caps": "that's too many capital letters.", "attachments": "that's too many files.",
    "spam": "slow down.", "warns": "that's too many warnings.", "copies": "that text was just posted several times.", "names": "that name isn't allowed here.",
}
# What the warn threshold can't do: it has no message to act on, and a warning there would count toward itself.
WARN_THRESHOLD_CANT = DELETE | WARN | CHANNEL_MESSAGE
# The order rules are checked in. Honeypot and invites come first because they catch spam accounts, which should get the heaviest answer.
SCAN_ORDER = ("honeypot", "invites", "words", "mentions", "links", "caps", "attachments", "copies", "spam")
# The rules that look at what a message says, which an edit can change.
EDIT_RULES = ("invites", "words", "links", "caps")

DISCORD_INVITE_HOSTS = ("discord.gg", "discord.com/invite", "discordapp.com/invite")
# Doubled slashes, backslashes and a /#/ all still reach the invite in a browser.
_INVITE = re.compile(r"(?:https?:[/\\]+)?(?:www\.)?(discord(?:app)?\.com[/\\]+invite|discord\s?\.\s?gg|dsc\.gg|discord\.me|discord\.io|discord\.li|discordy\.com|invite\.gg|disboard\.org[/\\]+server[/\\]+join)(?:/+#)?[/\\]+([a-zA-Z0-9-]+)", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DOMAIN = re.compile(r"[\w-]+(?:\.[\w-]+)+(?:/\S*)?")
_MENTION_OR_EMOJI = re.compile(r"<(?:@[!&]?|#|a?:\w+:)\d+>")
# Whitelist mode keeps out every site not named, and a `www.` address with no scheme still reads as a link to people.
_BARE_WWW = re.compile(r"(?<![\w.-])www\.[^\s<>\"']+\.[^\s<>\"']{2,}", re.IGNORECASE)
_INVITE_ENTRY = re.compile(r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|discord\.me|discord\.io|discord\.li|discordy\.com|invite\.gg|disboard\.org/server/join)/([\w-]+)/?", re.IGNORECASE)


def clean_punishment(value: Any) -> dict:
    clean = punishment(value)
    if sum(1 for bit, _ in HEAVY_ACTIONS if clean["actions"] & bit) > 1:
        raise Invalid("Pick only one of mute, tempmute, timeout, kick, tempban and ban.")
    return clean


def rate(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise Invalid("A rate is a count and a number of seconds.")
    return [integer(0, RATE_COUNT_MAX)(value[0]), integer(0, RATE_SECONDS_MAX)(value[1])]


def domain(value: Any) -> str:
    """A host and maybe a path. A pasted link works too: https://www.Example.com/a becomes example.com/a."""
    if not isinstance(value, str):
        raise Invalid("A link must be text.")
    cleaned = value.strip().lower()
    cleaned = re.sub(r"^[a-z]+://", "", cleaned)
    cleaned = re.sub(r"^www\.", "", cleaned).rstrip("/")
    if not _DOMAIN.fullmatch(cleaned) or len(cleaned) > 300:
        raise Invalid(f"`{value[:60]}` isn't a website, such as example.com.")
    return cleaned


def invite_entry(value: Any) -> str:
    """A server's ID, an invite link or a bare invite code. Codes and IDs both end up as text."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise Invalid("Give an invite link, an invite code or a server ID.")
    cleaned = value.strip()
    match = _INVITE_ENTRY.fullmatch(cleaned)
    if match:
        cleaned = match.group(1)
    if not re.fullmatch(r"[\w-]{1,64}", cleaned):
        raise Invalid(f"`{value[:60]}` isn't an invite link, an invite code or a server ID.")
    return cleaned


def pattern_problem(pattern: str) -> str | None:
    """Why a pattern can't be saved, in a sentence. Settings checks run on the bot's only thread, and compiling a short pattern can take seconds, so this only parses it."""
    try:
        regex_parser.parse(pattern)
    except (re.error, RecursionError, OverflowError) as error:
        return f"That pattern doesn't work: {error}"
    return None


def word_entry(value: Any) -> dict:
    """{"text": what to look for, "kind": substring, word or regex}. Plain text means a substring."""
    if isinstance(value, str):
        value = {"text": value, "kind": "substring"}
    if not isinstance(value, dict):
        raise Invalid("Each entry needs the text to look for.")
    kind = choice("substring", "word", "regex")(value.get("kind", "substring"))
    body = text(WORD_MAX if kind != "regex" else PATTERN_MAX, minimum=1)(value.get("text"))
    if kind == "regex":
        problem = pattern_problem(body)
        if problem:
            raise Invalid(problem)
    return {"text": body, "kind": kind}


def word_list(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise Invalid("Must be a list.")
    if len(value) > LIST_MAX:
        raise Invalid(f"Can hold at most {LIST_MAX} entries.")
    seen: set[tuple[str, str]] = set()
    out = []
    for item in value:
        entry = word_entry(item)
        key = (entry["text"].lower(), entry["kind"])
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


def text_list(check: Any, maximum: int = LIST_MAX) -> Any:
    def wrapped(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise Invalid("Must be a list.")
        if len(value) > maximum:
            raise Invalid(f"Can hold at most {maximum} entries.")
        return list(dict.fromkeys(check(item) for item in value))
    return wrapped


WARN_THRESHOLD_REFUSAL = "The warn threshold has no message to act on, and a warning there would count toward itself, so it can't delete, warn or post a message."


def warn_steps(value: Any) -> list[dict]:
    """More limits past the first, such as a mute above three warnings and a ban above five."""
    if not isinstance(value, list) or len(value) > WARN_STEPS_MAX:
        raise Invalid(f"Pick at most {WARN_STEPS_MAX} steps.")
    steps: dict[int, dict] = {}
    for item in value:
        if not isinstance(item, dict):
            raise Invalid("Each step needs a number of warnings and a punishment.")
        limit = integer(1, 1000)(item.get("threshold"))
        clean = clean_punishment(item.get("punishment"))
        if clean["actions"] & WARN_THRESHOLD_CANT:
            raise Invalid(WARN_THRESHOLD_REFUSAL)
        steps[limit] = {"threshold": limit, "punishment": clean}
    return [steps[limit] for limit in sorted(steps)]


def _rule(actions: int = DELETE, **fields: Any) -> Schema:
    return Schema({
        "enabled": Field(False, boolean),
        "punishment": Field({"actions": actions, "seconds": 0, "purge_days": 1}, clean_punishment),
        "dry_run": Field(False, boolean),
        "pause_seconds": Field(0, integer(0, 59)),  # under a minute, because a timeout that long or longer is a punishment with a case
        "exempt_channels": Field([], snowflakes(LIST_MAX)),
        "exempt_roles": Field([], snowflakes(LIST_MAX)),
        **fields,
    })


SETTINGS = Schema({
    "log_channel": Field(None, optional(snowflake)),
    "drama_channel": Field(None, optional(snowflake)),
    "allowed_channels": Field([], snowflakes(LIST_MAX)),
    "allowed_roles": Field([], snowflakes(LIST_MAX)),
    "media_channels": Field([], snowflakes(LIST_MAX)),
    "delete_files": Field(False, boolean),
    "general_dry_run": Field(False, boolean),
    "invites": _rule(no_role=Field(False, boolean), whitelist=Field([], text_list(invite_entry))),
    "mentions": _rule(rate=Field([10, 5], rate)),
    "attachments": _rule(rate=Field([5, 10], rate)),
    "caps": _rule(threshold=Field(70, integer(0, 100))),
    "honeypot": _rule(actions=DELETE | BAN, channels=Field([], snowflakes(LIST_MAX))),
    "links": _rule(mode=Field("blacklist", choice("blacklist", "whitelist", "no_role")), blacklist=Field([], text_list(domain)), whitelist=Field([], text_list(domain)), rate=Field([1, 1], rate)),
    "words": _rule(entries=Field([], word_list)),
    "spam": _rule(rate=Field([5, 5], rate)),
    # The same text from several accounts in one channel, which a per-person limit never sees.
    "copies": _rule(rate=Field([5, 15], rate)),
    "warns": _rule(actions=0, threshold=Field(0, integer(0, 1000)), steps=Field([], warn_steps)),
    # Names read against the bad word list, when someone joins and when they change one.
    "names": _rule(actions=0),
})


# Letters from other alphabets that read as Latin ones, which is how a word gets past a list.
_LOOKALIKE_LETTERS = {
    "a": "\u0430\u03b1", "b": "\u0432\u03b2", "c": "\u0441", "d": "\u0501\u0111", "e": "\u0435\u04bd\u03b5", "h": "\u043d\u04bb\u03b7\u0127", "i": "\u0456\u04cf\u03b9\u0131", "j": "\u0458",
    "k": "\u043a\u03ba", "l": "\u0142", "m": "\u043c\u03bc", "o": "\u043e\u03bf\u00f8", "p": "\u0440\u03c1", "q": "\u051b", "s": "\u0455", "t": "\u0442\u03c4", "u": "\u03c5",
    "v": "\u0475\u03bd", "w": "\u051d", "x": "\u0445\u03c7", "y": "\u0443\u04af\u03b3", "z": "\u03b6",
}
_LOOKALIKES = str.maketrans({other: latin for latin, others in _LOOKALIKE_LETTERS.items() for other in others})
# Less alike on their own, so they count only inside a word that already holds a Latin letter. Everywhere else they're ordinary Russian and Greek letters.
_WEAK_LOOKALIKES = str.maketrans({"\u0433": "r", "\u044f": "r", "\u0438": "u", "\u044c": "b", "\u03c3": "o"})
# Regional indicator letters spell a word as flags, and these letters render blank.
_REGIONAL_LETTERS = {0x1F1E6 + index: chr(ord("a") + index) for index in range(26)}
_BLANK_LETTERS = frozenset("\u3164\u115f\u1160\uffa0\u2800")
_HAS_LATIN = re.compile("[a-z]")
_SPACE_SPLIT = re.compile(r"(\s+)")
# Discord shows these as formatting, so sh__ip reads as ship.
_MARKUP = str.maketrans("", "", "_*~|`")
_SPACED_LETTERS = re.compile(r"(?<!\w)\w(?:\s+\w(?!\w)){3,}")
# What a browser's host mapping does that NFKC doesn't.
_HOST_MAP = str.maketrans({"\u3002": ".", "\uff61": ".", "\uff0e": ".", "\u00ad": None})


def _read_as_latin(token: str, foreign_words: bool) -> str:
    if _HAS_LATIN.search(token):
        return token.translate(_LOOKALIKES).translate(_WEAK_LOOKALIKES)
    return token if foreign_words else token.translate(_LOOKALIKES)


def fold(value: str, *, keep_punctuation: bool = False, foreign_words: bool = False) -> str:
    """Case, accents, lookalike letters and invisible characters don't matter. Punctuation doesn't either for a plain bad-word match, so b.o.a.t is boat.

    With `foreign_words`, a word with no Latin letter stays in its own alphabet, for matching against short English entries."""
    decomposed = unicodedata.normalize("NFKD", value).casefold().translate(_REGIONAL_LETTERS)
    if not decomposed.isascii():
        decomposed = "".join(_read_as_latin(part, foreign_words) if index % 2 == 0 else part for index, part in enumerate(_SPACE_SPLIT.split(decomposed)))
    out: list[str] = []
    for character in decomposed:
        category = unicodedata.category(character)
        if category in ("Mn", "Cf") or character in _BLANK_LETTERS:
            continue
        if keep_punctuation or category[0] in "LN" or character.isspace():
            out.append(" " if character.isspace() else character)
    return re.sub(r" +", " ", "".join(out)).strip()


def _join_spaced(text_: str) -> str:
    """`b o a t` reads as boat to everyone. Runs of four or more single letters close up."""
    return _SPACED_LETTERS.sub(lambda found: "".join(found.group(0).split()), text_)


def _short_latin(word: str) -> bool:
    return len(word) < WHOLE_SCRIPT_MIN_ENTRY and word.isascii()


class Compiled:
    """A server's rules prepared for the message path. Rebuilt only when its settings change."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.active = bool(cfg["media_channels"] or cfg["delete_files"] or any(cfg[name]["enabled"] for name in (*SCAN_ORDER, "warns", "names")))
        self.substrings: list[str] = []
        self.raw_substrings: list[str] = []
        self.patterns: list[str] = []
        whole: list[str] = []
        whole_short: list[str] = []
        for entry in cfg["words"]["entries"]:
            if entry["kind"] == "regex":
                self.patterns.append("(?i)" + entry["text"])
            elif entry["kind"] == "word":
                word = fold(entry["text"], keep_punctuation=True)
                if word:
                    (whole_short if _short_latin(word) else whole).append(re.escape(word))
            else:
                # Punctuation in an entry is part of what it looks for. Without it, `a$$` would come down to the letter a and match nearly every message.
                folded, kept = fold(entry["text"]), fold(entry["text"], keep_punctuation=True)
                if folded and folded == kept:
                    self.substrings.append(folded)
                elif kept:
                    self.raw_substrings.append(kept)
        self.whole_words = re.compile(rf"(?<!\w)(?:{'|'.join(whole)})(?!\w)") if whole else None
        self.short_whole_words = re.compile(rf"(?<!\w)(?:{'|'.join(whole_short)})(?!\w)") if whole_short else None
        self.link_blacklist = [split_domain(item) for item in cfg["links"]["blacklist"]]
        self.link_whitelist = [split_domain(item) for item in cfg["links"]["whitelist"]]
        hosts = [re.escape(item) for item in cfg["links"]["blacklist"]]
        self.bare_blacklist = re.compile(rf"(?<![\w.-])(?:{'|'.join(hosts)})(?![\w-]|\.[\w-])", re.IGNORECASE) if hosts else None
        self.invite_whitelist = {item.lower() for item in cfg["invites"]["whitelist"]}


def split_domain(entry: str) -> tuple[str, str]:
    host, _, path = entry.partition("/")
    return host, "/" + path if path else ""


def link_matches(host: str, path: str, entries: list[tuple[str, str]]) -> bool:
    """Whether a link sits under one of these. A subdomain counts, but example.com.evil.net doesn't count as example.com."""
    for entry_host, entry_path in entries:
        if (host == entry_host or host.endswith("." + entry_host)) and path.startswith(entry_path):
            return True
    return False


def decoded(body: str) -> str:
    """Percent escapes undone, since a browser undoes them and a filter has to read what it reads."""
    return unicodedata.normalize("NFC", unquote(body))


def unmasked(text_: str) -> str:
    """Text with a host's disguises taken off: full-width letters, other alphabets' full stops, soft hyphens."""
    return unicodedata.normalize("NFKC", text_).translate(_HOST_MAP)


def _plain_host(host: str) -> str:
    """How a browser reads a host: full-width letters are plain ones, other full stops are dots, invisible characters are gone, and a trailing dot changes nothing.

    A site with real letters from another alphabet keeps them, because that's how a person types it on a list."""
    plain = unmasked(host).rstrip(".")
    try:
        ascii_form = plain.encode("idna").decode()
    except UnicodeError:
        ascii_form = None
    if ascii_form is not None and "xn--" not in ascii_form:
        plain = ascii_form
    return re.sub(r"^www\.", "", plain)


def hosts_in(body: str) -> list[tuple[str, str]]:
    found = []
    for url in _URL.findall(body):
        try:
            parts = urlsplit(url.rstrip(").,>"))
            host = (parts.hostname or "").lower()
            before_at = (parts.username or "").lower()
        except ValueError:
            continue
        if host:
            found.append((_plain_host(host), parts.path.lower()))
        # A site named before an @ counts too. With a backslash in front of the @, a browser goes there instead.
        if "." in before_at:
            found.append((_plain_host(before_at), parts.path.lower()))
    return found


def caps_percent(body: str) -> tuple[int, int]:
    """(percent uppercase, letters counted). Mentions, emoji and links don't count, because a member didn't choose their capitals."""
    stripped = _URL.sub("", _MENTION_OR_EMOJI.sub("", body))
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return 0, 0
    return round(100 * sum(1 for c in letters if c.isupper()) / len(letters)), len(letters)


def describe_punishment(value: dict) -> str:
    names = []
    for bit, name in ACTION_NAMES:
        if value["actions"] & bit:
            names.append(f"{name} {humanize(value['seconds'])}" if bit & TIMED else name)
    return ", ".join(names) or "nothing"


def parse_punishment(words: str, *, keep_purge_days: int = 0) -> dict:
    """`delete, tempmute 3h, warn` as a punishment. Commas are optional."""
    parts = [part for part in re.split(r"[,\s]+", words.strip().lower()) if part]
    actions = 0
    seconds = 0
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        bit = PUNISHMENT_WORDS.get(part)
        if bit is None:
            raise Invalid(f"`{part[:30]}` isn't a punishment. Pick from delete, warn, tempmute, mute, timeout, kick, tempban, ban, defer, message and dm.")
        actions |= bit
        if bit & TIMED:
            length = parse_duration(parts[index]) if index < len(parts) else None
            if length:
                seconds = length
                index += 1
            elif bit == TIMEOUT and not seconds:
                seconds = 3600
            elif not seconds:
                raise Invalid(f"{part} needs a length of time, such as {part} 3h.")
    return clean_punishment({"actions": actions, "seconds": seconds, "purge_days": keep_purge_days})


def parse_rate(words: str, one_means: str = "per") -> list[int]:
    """`5 25` is five in 25 seconds. A single number N is one every N seconds, or with one_means="count", N in one second."""
    parts = [part for part in re.split(r"[\s/,]+", words.strip()) if part]
    try:
        numbers = [int(part) for part in parts]
    except ValueError as error:
        raise Invalid("Give a count and a number of seconds, such as 5 25.") from error
    if len(numbers) == 1:
        numbers = [numbers[0], 1] if one_means == "count" else [1, numbers[0]]
    if len(numbers) != 2 or min(numbers) < 1:
        raise Invalid("Give a count and a number of seconds, both at least 1, such as 5 25.")
    return rate(numbers)


def split_words(words: str) -> list[str]:
    return [part.strip(",") for part in split_quoted(words) if part.strip(",")]


def _overlay(base: dict, form: dict) -> dict:
    """The form's values over the defaults, section by section."""
    out = dict(base)
    for name, value in form.items():
        out[name] = _overlay(out[name], value) if isinstance(value, dict) and isinstance(out.get(name), dict) else value
    return out


# Which bit of the old setup's disabled_features turned off each rule.
EXPORT_DISABLED = {"attachments": 1, "invites": 2, "mentions": 4, "honeypot": 8, "words": 16, "caps": 32, "links": 64, "spam": 128, "warns": 256}


def settings_from_export(data: dict, channel_ids: set[int], role_ids: set[int]) -> dict:
    """A settings form from the old setup's automod export. A channel or role that isn't in this server is left out, and so is an entry that can't be read.

    There, a rule with nothing set up did nothing, whatever its switch said. Here a rule is either on or off, so one is on only if it was on and set up.
    """
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else data
    disabled = settings.get("disabled_features") if isinstance(settings.get("disabled_features"), int) else 0
    form: dict[str, Any] = {"delete_files": settings.get("delete_files") is True}

    def known(values: Any, allowed: set[int]) -> list[int]:
        found = []
        for value in values if isinstance(values, list) else []:
            try:
                item = snowflake(value)
            except Invalid:
                continue
            if item in allowed:
                found.append(item)
        return found

    def good(values: Any, check: Any) -> list:
        found = []
        for value in values if isinstance(values, list) else []:
            try:
                found.append(check(value))
            except Invalid:
                continue
        return found

    def rule(name: str, source: str, configured: bool, **fields: Any) -> None:
        part = {"enabled": configured and not disabled & EXPORT_DISABLED[name], **fields}
        raw = settings.get(source)
        if isinstance(raw, list) and raw:
            try:
                part["punishment"] = clean_punishment({"actions": raw[0], "seconds": raw[1] if len(raw) > 1 and raw[1] else 0, "purge_days": raw[2] if len(raw) > 2 and raw[2] is not None else 1})
                if name == "warns":
                    part["punishment"]["actions"] &= ~WARN_THRESHOLD_CANT
            except Invalid:
                part["enabled"] = False
        form[name] = part

    def rate_of(source: str) -> list[int] | None:
        try:
            value = rate(settings.get(source))
        except Invalid:
            return None
        return value if value[0] and value[1] else None

    for field_name, source in (("log_channel", "log_channel"), ("drama_channel", "council_channel")):
        found = known([settings.get(source)], channel_ids)
        if found:
            form[field_name] = found[0]
    form["allowed_channels"] = known(settings.get("channel_wl"), channel_ids)
    form["allowed_roles"] = known(settings.get("role_wl"), role_ids)
    form["media_channels"] = known(settings.get("media_channels"), channel_ids)

    honeypot = known(settings.get("honeypot_channels"), channel_ids)
    rule("honeypot", "honeypot_punishment", bool(honeypot), channels=honeypot)
    rule("invites", "invite_punishment", settings.get("isInviteSpamEnabled") is True, no_role=settings.get("isNoroleModeEnabled") is True, whitelist=good(settings.get("invite_wl"), invite_entry))
    words = good(settings.get("bad_words"), word_entry)
    rule("words", "word_punishment", bool(words), entries=words)
    for name, source, punished in (("mentions", "mentionspam", "mentionspam_punishment"), ("attachments", "attachmentspam", "attachment_punishment"), ("spam", "slowmode", "slowmode_punishment")):
        found_rate = rate_of(source)
        rule(name, punished, found_rate is not None, **({"rate": found_rate} if found_rate else {}))
    link_rate = rate_of("link_slowmode")
    mode = {0: "blacklist", 1: "whitelist", 2: "no_role"}.get(settings.get("link_setting"), "blacklist")
    rule("links", "link_punishment", link_rate is not None, mode=mode, blacklist=good(settings.get("link_bl"), domain), whitelist=good(settings.get("link_wl"), domain), **({"rate": link_rate} if link_rate else {}))
    caps = settings.get("caps_threshold")
    caps = caps if isinstance(caps, int) and not isinstance(caps, bool) and 0 < caps <= 100 else 0
    rule("caps", "caps_punishment", bool(caps), **({"threshold": caps} if caps else {}))
    threshold = settings.get("warn_threshold")
    threshold = threshold if isinstance(threshold, int) and not isinstance(threshold, bool) and 0 < threshold <= 1000 else 0
    rule("warns", "warn_punishment", bool(threshold), threshold=threshold)
    return form


class DramaView(discord.ui.View):
    """Buttons under an incident in the drama channel. Which incident comes from the message they're on, so they keep working after a restart."""

    def __init__(self, cog: "Automod") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, custom_id="automod:drama:ban")
    async def ban(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.drama_action(interaction, "ban")

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.danger, custom_id="automod:drama:kick")
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.drama_action(interaction, "kick")

    @discord.ui.button(label="Mute", style=discord.ButtonStyle.primary, custom_id="automod:drama:mute")
    async def mute(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.drama_action(interaction, "mute")

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, custom_id="automod:drama:dismiss")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.drama_action(interaction, "dismiss")


def closed_drama_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, style in (("Ban", discord.ButtonStyle.danger), ("Kick", discord.ButtonStyle.danger), ("Mute", discord.ButtonStyle.primary), ("Dismiss", discord.ButtonStyle.secondary)):
        view.add_item(discord.ui.Button(label=label, style=style, disabled=True, custom_id=f"automod:drama:{label.lower()}"))
    return view


DRAMA_DONE = {"ban": "banned them", "kick": "kicked them", "mute": "muted them", "dismiss": "dismissed this"}
DRAMA_NEEDS = {"ban": ("ban", "ban_members"), "kick": ("kick", "kick_members"), "mute": ("mute", "manage_roles")}


class Automod(Module):
    key = "automod"
    category = "Automod"
    schema = SETTINGS
    migrations = [
        """
        CREATE TABLE drama_incidents (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            rule TEXT NOT NULL,
            source_channel_id INTEGER,
            source_message_id INTEGER,
            created_at REAL NOT NULL,
            handled_at REAL,
            handled_by INTEGER,
            handled_by_name TEXT,
            outcome TEXT,
            PRIMARY KEY (guild_id, message_id)
        );
        CREATE INDEX drama_open ON drama_incidents (guild_id, handled_at);
        """,
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.regex = RegexGuard()
        self._compiled: dict[int, Compiled] = {}
        self._windows: dict[tuple[int, int, str, int], deque[tuple[float, int]]] = {}
        self._last_fired: dict[tuple[int, int, str], float] = {}
        self._invites: dict[str, tuple[int | None, bool, float]] = {}
        self._copies: dict[tuple[int, int, int, int], deque[tuple[float, int]]] = {}
        self._edits_seen: dict[int, Any] = {}
        self._last_sweep = time.monotonic()

    async def on_load(self) -> None:
        self.bot.add_view(DramaView(self))

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        """Log entries and incidents carry what members wrote."""
        return {name: {settings[name]} if settings.get(name) else set() for name in ("log_channel", "drama_channel")}

    def reading_refusal(self, member: Any, before: dict, after: dict) -> tuple[str, str] | None:
        """These logs show what a rule caught in any channel, and a rule can be set to catch anything without deleting it, so someone who can't read a channel the rules cover can't aim them somewhere."""
        for field in ("log_channel", "drama_channel"):
            if after.get(field) and after[field] != before.get(field):
                covered = set(after["allowed_channels"])
                if not access.reads_all_but(member, lambda channel: channel.id in covered or getattr(channel, "category_id", None) in covered):
                    return field, access.READS_ALL
        return None

    async def cog_unload(self) -> None:
        await self.regex.close()

    def _rules(self, guild_id: int) -> Compiled:
        cfg = self.cfg(guild_id)
        compiled = self._compiled.get(guild_id)
        if compiled is None or compiled.cfg is not cfg:
            compiled = Compiled(cfg)
            self._compiled[guild_id] = compiled
        return compiled

    # ---- reading a message ----

    @staticmethod
    def _body(message: Any) -> str:
        """What a message says, including a poll's text and what it forwards, because those are how filters get dodged."""
        parts = [message.content or ""]
        poll = getattr(message, "poll", None)
        if poll is not None:
            parts.append(getattr(poll, "question", "") or "")
            parts.extend(getattr(answer, "text", "") or "" for answer in getattr(poll, "answers", None) or [])
        for snapshot in getattr(message, "message_snapshots", None) or []:
            parts.append(getattr(snapshot, "content", "") or "")
            for embed in getattr(snapshot, "embeds", None) or []:
                parts.extend((embed.title or "", embed.description or ""))
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _files(message: Any) -> list[Any]:
        files = list(message.attachments)
        for snapshot in getattr(message, "message_snapshots", None) or []:
            files.extend(getattr(snapshot, "attachments", None) or [])
        return files

    @staticmethod
    def _places(channel: Any) -> set[int]:
        places = {channel.id}
        parent = getattr(channel, "parent", None)
        if parent is not None:
            places.add(parent.id)
            if getattr(parent, "category_id", None):
                places.add(parent.category_id)
        if getattr(channel, "category_id", None):
            places.add(channel.category_id)
        return places

    def _immune(self, member: Any) -> bool:
        permissions = member.guild_permissions
        if permissions.manage_guild or permissions.administrator or permissions.ban_members or permissions.kick_members or permissions.manage_messages:
            return True
        manager = self.bot.settings.get(member.guild.id, COMMANDS_MODULE)["mod_role"]
        return bool(manager) and any(role.id == manager for role in member.roles)

    @staticmethod
    def _rule_exempt(rule: dict, places: set[int], role_ids: set[int]) -> bool:
        return bool(places.intersection(rule["exempt_channels"]) or role_ids.intersection(rule["exempt_roles"]))

    def _tally(self, guild_id: int, user_id: int, rule: str, amount: int, seconds: int, *, keep: int | None = None) -> int:
        """Add to this person's count for the last `seconds`, and return it. `keep` caps what a long window holds, where each entry counts one and more than that says nothing new."""
        now = time.monotonic()
        if now - self._last_sweep > WINDOW_SWEEP_SECONDS:
            self._sweep(now)
        key = (guild_id, user_id, rule, seconds)
        window = self._windows.get(key)
        if window is None or window.maxlen != keep:
            window = self._windows[key] = deque(window or (), maxlen=keep)
        if amount:
            window.append((now, amount))
        while window and window[0][0] <= now - seconds:
            window.popleft()
        return sum(count for _, count in window)

    def _sweep(self, now: float) -> None:
        """Forget anyone who hasn't posted lately, so these stay small on a big server."""
        self._last_sweep = now
        self._windows = {key: window for key, window in self._windows.items() if window and window[-1][0] > now - key[3]}
        self._copies = {key: window for key, window in self._copies.items() if window and window[-1][0] > now - key[3]}
        self._last_fired = {key: at for key, at in self._last_fired.items() if at > now - 3600}

    async def _invite_guild(self, code: str) -> tuple[int | None, bool]:
        """(server ID, whether the invite exists). A failed lookup counts as a live invite to somewhere else, so an outage can't let spam through."""
        now = time.monotonic()
        cached = self._invites.get(code)
        if cached and cached[2] > now:
            return cached[0], cached[1]
        try:
            invite = await self.bot.fetch_invite(code, with_counts=False, with_expiration=False)
        except discord.NotFound:
            found: tuple[int | None, bool] = (None, False)
        except Exception as error:
            log.info("Could not look up invite %s: %s", code, error)
            return None, True
        else:
            found = (getattr(getattr(invite, "guild", None), "id", None), True)
        if len(self._invites) >= INVITE_CACHE_MAX:
            self._invites.clear()
        self._invites[code] = (*found, now + INVITE_CACHE_SECONDS)
        return found

    async def _foreign_invite(self, guild: Any, body: str, compiled: Compiled) -> str | None:
        """The first invite to anywhere but this server, unless the whitelist names it. A code nobody has looked up lately spends one of a few lookups, and past those it counts as foreign."""
        whitelist = compiled.invite_whitelist
        vanity = (getattr(guild, "vanity_url_code", None) or "").lower()
        # Discord's markdown hides a backslash in front of a dot. The pattern already takes one where a slash goes.
        plain = decoded(body).replace("\\.", ".")
        bare = unmasked(plain)
        seen: set[tuple[str, str]] = set()
        lookups = 0
        for source in (plain, bare) if bare != plain else (plain,):
            for match in _INVITE.finditer(source):
                host, code = re.sub(r"[/\\]+", "/", re.sub(r"\s", "", match.group(1))).lower(), match.group(2)
                if (host, code) in seen:
                    continue
                seen.add((host, code))
                if code.lower() in whitelist:
                    continue
                # Other invite sites redirect somewhere only they know, so those can't be checked and always count.
                if host in DISCORD_INVITE_HOSTS:
                    if code.lower() == vanity:
                        continue
                    if not self._invite_known(code):
                        lookups += 1
                        if lookups > INVITE_LOOKUPS_PER_MESSAGE:
                            return match.group(0)
                    server, alive = await self._invite_guild(code)
                    if not alive or server == guild.id or (server is not None and str(server) in whitelist):
                        continue
                return match.group(0)
        return None

    def _invite_known(self, code: str) -> bool:
        cached = self._invites.get(code)
        return cached is not None and cached[2] > time.monotonic()

    @staticmethod
    def _word_hit(compiled: Compiled, body: str) -> str | None:
        """The first plain entry this text holds. A word in another alphabet can fold to a short English one, so a short entry reads a copy that leaves those words as they are."""
        foreign = body.isascii() is False
        if compiled.substrings:
            folded, folded_own = fold(body), fold(body, foreign_words=True) if foreign else None
            texts = (folded, _join_spaced(folded))
            short_texts = texts if not foreign else (folded_own, _join_spaced(folded_own))
            for word in compiled.substrings:
                if any(word in text_ for text_ in (short_texts if _short_latin(word) else texts)):
                    return word
        if compiled.raw_substrings or compiled.whole_words is not None or compiled.short_whole_words is not None:
            kept = fold(body, keep_punctuation=True)
            kept_own = fold(body, keep_punctuation=True, foreign_words=True) if foreign else kept
            for word in compiled.raw_substrings:
                if word in (kept_own if _short_latin(word) else kept):
                    return word
            # An entry holding one of the markup characters matches the text as typed. Every other entry matches past them.
            if compiled.whole_words is not None:
                found = compiled.whole_words.search(kept) or compiled.whole_words.search(_join_spaced(kept.translate(_MARKUP)))
                if found:
                    return found.group(0)
            if compiled.short_whole_words is not None:
                found = compiled.short_whole_words.search(kept_own) or compiled.short_whole_words.search(_join_spaced(kept_own.translate(_MARKUP)))
                if found:
                    return found.group(0)
        return None

    async def _bad_word(self, compiled: Compiled, guild: Any, body: str) -> str | None:
        hit = self._word_hit(compiled, body)
        if hit is not None:
            return hit
        if not compiled.patterns:
            return None
        # A pattern reads the text as typed and the text with accents, lookalike letters and invisible characters taken off. The second round trip happens only when folding changed something.
        lowered = body.casefold()
        folded = fold(body, keep_punctuation=True)
        for pattern in compiled.patterns:
            for text_ in (body, *((folded,) if folded != lowered else ())):
                if await self.regex.search(pattern, text_, server=guild.id):
                    return f"pattern {pattern[4:]}"
                if self.regex.first_slow_notice(pattern, guild.id):
                    self._log_slow_pattern(guild, pattern[4:])
        return None

    async def word_problem(self, member: Any, text: str) -> str | None:
        """Why someone can't save text the bot will post for them later, or None. What the bot posts never passes the scan, so the word rules meet it here. A channel's exemption doesn't count, since the text can run anywhere."""
        compiled = self._rules(member.guild.id)
        cfg = compiled.cfg
        rule = cfg["words"]
        role_ids = {role.id for role in member.roles}
        if not rule["enabled"] or rule["dry_run"] or self._immune(member) or role_ids.intersection(cfg["allowed_roles"]) or role_ids.intersection(rule["exempt_roles"]):
            return None
        # A pattern only reads so much at a time, and a tag can run far longer than a message, so long text goes through in overlapping pieces.
        for start in range(0, len(text) or 1, SAVED_TEXT_STEP):
            if await self._bad_word(compiled, member.guild, text[start:start + SAVED_TEXT_PIECE]) is not None:
                return "This server's automod doesn't allow some of that text."
        return None

    async def link_problem(self, member: Any, text: str) -> str | None:
        """Why the bot won't post these links or invites for someone, or None. As with word_problem, a channel's exemption doesn't count."""
        compiled = self._rules(member.guild.id)
        cfg = compiled.cfg
        role_ids = {role.id for role in member.roles}
        if self._immune(member) or role_ids.intersection(cfg["allowed_roles"]):
            return None
        rule = cfg["links"]
        if rule["enabled"] and not rule["dry_run"] and all(rule["rate"]) and not role_ids.intersection(rule["exempt_roles"]) and self._bad_link(compiled, member, text, rule)[0]:
            return "This server's automod doesn't allow a link in that."
        rule = cfg["invites"]
        if rule["enabled"] and not rule["dry_run"] and not role_ids.intersection(rule["exempt_roles"]) and not (rule["no_role"] and len(member.roles) > 1):
            if await self._foreign_invite(member.guild, text, compiled):
                return "This server's automod doesn't allow an invite in that."
        return None

    async def output_problem(self, member: Any, text: str) -> str | None:
        """Why the bot won't post what a member's template produced when `member` ran it, or None. Blocks can build at run time what the check at saving never saw."""
        return await self.word_problem(member, text) or await self.link_problem(member, text)

    def _bad_link(self, compiled: Compiled, member: Any, body: str, rule: dict) -> tuple[int, str | None]:
        """How many links in this message count against the member, and the first of them. Backslashes go first, since Discord's markdown hides them."""
        body = decoded(body).replace("\\", "")
        hosts = hosts_in(body)
        mode = rule["mode"]
        if mode == "blacklist":
            bad = [item for item in hosts if link_matches(item[0], item[1], compiled.link_blacklist)]
            # A blacklisted site typed without https:// isn't a link to Discord, and it's still the thing the server asked to keep out.
            if not bad and compiled.bare_blacklist is not None:
                found = compiled.bare_blacklist.search(_URL.sub(" ", body))
                if found:
                    return 1, found.group(0)
        else:
            if mode == "no_role" and len(member.roles) > 1:
                return 0, None
            bad = [item for item in [*hosts, *self._bare_www_hosts(body)] if not link_matches(item[0], item[1], compiled.link_whitelist)]
        return len(bad), (bad[0][0] + bad[0][1]) if bad else None

    @staticmethod
    def _bare_www_hosts(body: str) -> list[tuple[str, str]]:
        """`www.` addresses with no scheme, read the way hosts_in reads a link."""
        return hosts_in(" ".join(f"https://{found}" for found in _BARE_WWW.findall(_URL.sub(" ", body))))

    # ---- the message path ----

    async def _check_rule(self, name: str, rule: dict, message: Any, member: Any, compiled: Compiled, body: str, places: set[int], edited: bool) -> str | None:
        """Why this rule trips on this message, or None. Rate rules count the message as they look at it."""
        guild = message.guild
        if name == "honeypot":
            return "Posted in a honeypot channel" if places.intersection(rule["channels"]) else None
        if name == "invites":
            if rule["no_role"] and len(member.roles) > 1:
                return None
            found = await self._foreign_invite(guild, body, compiled)
            return f"Invite: {found}" if found else None
        if name == "words":
            found = await self._bad_word(compiled, guild, body)
            return f"Matched: {found}" if found else None
        if name == "mentions":
            count, seconds = rule["rate"]
            if not count or not seconds:
                return None
            amount = len(message.mentions) + len(message.role_mentions) + (1 if message.mention_everyone else 0)
            if not amount:
                return None
            total = self._tally(guild.id, member.id, name, amount, seconds)
            return f"{total} mentions in {seconds} seconds" if total >= count else None
        if name == "links":
            count, seconds = rule["rate"]
            if not count or not seconds:
                return None
            amount, first = self._bad_link(compiled, member, body, rule)
            if not amount:
                return None
            if edited:
                return f"Link: {first}"
            total = self._tally(guild.id, member.id, name, amount, seconds)
            return f"Link: {first}" if total >= count else None
        if name == "caps":
            if not rule["threshold"] or len(body) < CAPS_MIN_LENGTH:
                return None
            percent, letters = caps_percent(body)
            return f"{percent}% capitals" if letters >= CAPS_MIN_LENGTH and percent >= rule["threshold"] else None
        if name == "attachments":
            count, seconds = rule["rate"]
            amount = len(self._files(message))
            if not count or not seconds or not amount:
                return None
            total = self._tally(guild.id, member.id, name, amount, seconds)
            return f"{total} files in {seconds} seconds" if total > count else None
        if name == "spam":
            count, seconds = rule["rate"]
            if not count or not seconds:
                return None
            total = self._tally(guild.id, member.id, name, 1, seconds, keep=count + 1)
            return f"{total} messages in {seconds} seconds" if total > count else None
        if name == "copies":
            count, seconds = rule["rate"]
            if not count or not seconds:
                return None
            total = self._tally_copy(message, body, count, seconds)
            return f"{total} copies of one text in {seconds} seconds" if total >= count else None
        return None

    def _copy_window(self, message: Any, body: str, seconds: int) -> tuple[Any, ...] | None:
        folded = fold(body)
        return (message.guild.id, message.channel.id, hash(folded), seconds) if len(folded) >= COPY_MIN_LENGTH else None

    def _tally_copy(self, message: Any, body: str, count: int, seconds: int) -> int:
        """How many times one text went into this channel lately, whoever posted it, as accounts in a raid take turns to stay under a per-person limit."""
        key = self._copy_window(message, body, seconds)
        if key is None:
            return 0
        now = time.monotonic()
        if now - self._last_sweep > WINDOW_SWEEP_SECONDS:
            self._sweep(now)
        window = self._copies.get(key)
        if window is None or window.maxlen != count:
            window = self._copies[key] = deque(window or (), maxlen=count)
        window.append((now, message.id))
        while window and window[0][0] <= now - seconds:
            window.popleft()
        return len(window)

    async def _delete_copies(self, message: Any, body: str, seconds: int) -> None:
        """The copies posted before the limit tripped are the same spam, and they go too."""
        for _, message_id in list(self._copies.get(self._copy_window(message, body, seconds), ())):
            if message_id != message.id:
                try:
                    await message.channel.get_partial_message(message_id).delete()
                except SEND_ERRORS as error:
                    log.info("Could not delete a copy in %s: %s", message.channel.id, error)

    async def _scan(self, message: Any, member: Any, *, edited: bool = False) -> None:
        guild = message.guild
        compiled = self._rules(guild.id)
        cfg = compiled.cfg
        if not compiled.active or self._immune(member):
            return
        places = self._places(message.channel)
        role_ids = {role.id for role in member.roles}
        if places.intersection(cfg["allowed_channels"]) or role_ids.intersection(cfg["allowed_roles"]):
            return
        body = self._body(message)
        for name in EDIT_RULES if edited else SCAN_ORDER:
            rule = cfg[name]
            if not rule["enabled"] or self._rule_exempt(rule, places, role_ids):
                continue
            detail = await self._check_rule(name, rule, message, member, compiled, body, places, edited)
            if detail is None:
                continue
            before = await self._text_before(message, body) if edited else None
            await self.enforce(guild, member, name, rule, detail, message=message, edited=edited, before=before)
            if not rule["dry_run"]:
                return
        if not edited:
            await self._check_channel_rules(message, member, cfg, places, body)

    async def _text_before(self, message: Any, body: str) -> str | None:
        """What an edited message said before, when the logging feature kept it."""
        logs = self.bot.module("logging")
        if logs is None:
            return None
        stored = await logs.stored_message(message.id)
        text_ = (stored or {}).get("content")
        return text_ if text_ and text_ != body else None

    async def _check_channel_rules(self, message: Any, member: Any, cfg: dict, places: set[int], body: str) -> None:
        """Media-only channels and unsafe files have no punishment of their own. The message goes, and that's logged."""
        files = self._files(message)
        name = None
        # Talk in a thread under a media-only channel is what the thread is for. Without Message Content every message arrives empty, pictures included, so this can't tell and stands down.
        in_thread = getattr(message.channel, "parent", None) is not None
        if self.bot.reads_message_text and not in_thread and places.intersection(cfg["media_channels"]) and not files and not message.embeds and not _URL.search(body):
            name, detail = "media", "Posted text in a media-only channel"
        elif cfg["delete_files"]:
            unsafe = [f.filename for f in files if f.filename.rsplit(".", 1)[-1].lower() not in SAFE_FILE_TYPES or "." not in f.filename]
            if unsafe:
                name, detail = "files", "Unsafe file: " + ", ".join(unsafe[:5])
        if name is None:
            return
        dry = cfg["general_dry_run"]
        result = "Would delete the message" if dry else await self._delete(message)
        self._log(message.guild, member, name, detail, [result], message=message, dry_run=dry)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        member = message.author
        if not hasattr(member, "guild_permissions"):
            return
        await self._scan(message, member)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Editing a link into a message that passed must not dodge the filters. Discord also sends this when it adds a link's preview, and then the edit time doesn't change."""
        message = payload.message
        if payload.guild_id is None or message.edited_at is None or message.author.bot or message.webhook_id is not None:
            return
        if self._edits_seen.get(message.id) == message.edited_at:
            return
        if len(self._edits_seen) >= EDITS_REMEMBERED:
            self._edits_seen.clear()
        self._edits_seen[message.id] = message.edited_at
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or message.guild is None:
            return
        member = message.author if hasattr(message.author, "guild_permissions") else guild.get_member(message.author.id)
        if member is not None:
            await self._scan(message, member, edited=True)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: Any) -> None:
        """A thread or forum post's title reads like a message to everyone in the channel, and no message event carries it."""
        guild = thread.guild
        member = guild.get_member(thread.owner_id) if thread.owner_id else None
        if member is None or member.bot:
            return
        compiled = self._rules(guild.id)
        cfg = compiled.cfg
        if not compiled.active or self._immune(member):
            return
        places = self._places(thread)
        role_ids = {role.id for role in member.roles}
        if places.intersection(cfg["allowed_channels"]) or role_ids.intersection(cfg["allowed_roles"]):
            return
        # An edit doesn't count toward a rate, and neither does a title.
        stand_in = SimpleNamespace(guild=guild)
        for name in ("invites", "words", "links"):
            rule = cfg[name]
            if not rule["enabled"] or self._rule_exempt(rule, places, role_ids):
                continue
            detail = await self._check_rule(name, rule, stand_in, member, compiled, thread.name, places, True)
            if detail is None:
                continue
            await self.enforce(guild, member, name, rule, f"Thread title in {thread.mention}: {detail}", thread=thread)
            if not rule["dry_run"]:
                return

    async def _check_name(self, member: Any) -> None:
        """Read someone's names against the bad word list. A name shows wherever they post, so it can say what a message can't."""
        guild = member.guild
        compiled = self._rules(guild.id)
        cfg = compiled.cfg
        rule = cfg["names"]
        if not rule["enabled"] or member.bot or self._immune(member):
            return
        role_ids = {role.id for role in member.roles}
        if role_ids.intersection(cfg["allowed_roles"]) or role_ids.intersection(rule["exempt_roles"]):
            return
        for name in dict.fromkeys(part for part in (member.nick, member.global_name, member.name) if part):
            found = await self._bad_word(compiled, guild, name)
            if found:
                await self.enforce(guild, member, "names", rule, f"Name {discord.utils.escape_markdown(name)}. Matched: {found}")
                return

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._check_name(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick:
            await self._check_name(after)

    @commands.Cog.listener()
    async def on_user_update(self, before: Any, after: Any) -> None:
        if before.name == after.name and before.global_name == after.global_name:
            return
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is not None:
                await self._check_name(member)

    # ---- acting ----

    async def _delete(self, message: Any) -> str:
        try:
            await message.delete()
        except discord.NotFound:
            return "Message was already gone"
        except discord.HTTPException as error:
            return f"Couldn't delete the message: {error.text or error.status}"
        return "Deleted the message"

    async def enforce(self, guild: Any, member: Any, name: str, rule: dict, detail: str, *, message: Any = None, source: str = "automod", thread: Any = None, edited: bool = False, before: str | None = None, repeat_as: str | None = None) -> None:
        """Carry out a rule's punishment and log it. In dry-run mode only the log entry happens. `thread` is one whose title tripped the rule."""
        punishment_ = rule["punishment"]
        actions = punishment_["actions"]
        # Member text can be most of a message long, and Discord refuses a log entry past its limit.
        detail = detail if len(detail) <= LOG_DETAIL_MAX else detail[:LOG_DETAIL_MAX - 1] + "\N{HORIZONTAL ELLIPSIS}"
        if rule["dry_run"]:
            self._log(guild, member, name, detail, [f"Would {describe_punishment(punishment_)}"], message=message, dry_run=True, edited=edited, before=before)
            return
        results: list[str] = []
        # A message over the rate limit goes even if the punishment doesn't say so. Letting it stand would defeat the limit.
        if message is not None and (actions & DELETE or name == "spam"):
            results.append(await self._delete(message))
        if name == "copies" and message is not None and actions & DELETE:
            await self._delete_copies(message, self._body(message), rule["rate"][1])
        if thread is not None and actions & DELETE:
            results.append(await self._delete_thread(thread))
        if rule.get("pause_seconds") and message is not None and not actions & (BAN | TEMPBAN | KICK):
            results.append(await self._pause_sender(guild, member, rule["pause_seconds"], f"Automod: {RULE_LABELS[name]}"))
        now = time.monotonic()
        key = (guild.id, member.id, repeat_as or name)
        window = max(REPEAT_SECONDS, rule["rate"][1] if "rate" in rule else 0)
        repeat = now - self._last_fired.get(key, float("-inf")) < window
        if repeat:
            # The rate rules would flood the log. These three carry what someone said, and staff see each attempt.
            if name in ("invites", "words", "links"):
                self._log(guild, member, name, detail, [*results, "No new punishment, since this rule just fired for them"], message=message, dry_run=False, edited=edited, before=before)
            return
        self._last_fired[key] = now
        reason = f"Automod: {RULE_LABELS[name]}"
        heavy = next((word for bit, word in HEAVY_ACTIONS if actions & bit), None)
        heavy_bit = next((bit for bit, word in HEAVY_ACTIONS if actions & bit), 0)
        seconds = punishment_["seconds"] if heavy_bit & TIMED else 0
        # Once someone is banned or kicked, Discord refuses a DM, so that one goes first and comes back if the action fails. Anything else goes after, so it says what really happened.
        dm_first = bool(actions & DM_OFFENDER) and heavy in ("ban", "tempban", "kick")
        sent: list[Any] = []
        if dm_first:
            results.append(await self._dm_offender(guild, member, name, heavy, seconds, keep=sent))
        moderation = self.bot.module("moderation")
        tell = not actions & DM_OFFENDER
        outcome = None
        if moderation is None:
            if actions & (WARN | heavy_bit):
                results.append("Moderation isn't loaded, so no punishment was given")
        else:
            if actions & WARN and name != "warns":
                results.append(await self._punish(moderation, guild, member, "warn", reason, None, 0, tell, source))
            if heavy:
                outcome = await self._punish(moderation, guild, member, heavy, reason, seconds, punishment_["purge_days"], tell, source)
                results.append(outcome)
                if dm_first and outcome.startswith(DIDNT_LAND):
                    await moderation.take_back(sent)
        if actions & DM_OFFENDER and not dm_first:
            results.append(await self._dm_offender(guild, member, name, heavy if outcome and not outcome.startswith(DIDNT_LAND) else None, seconds))
        if actions & CHANNEL_MESSAGE and message is not None:
            results.append(await self._notice(message, member, name))
        if actions & DRAMA:
            results.append(await self._post_drama(guild, member, name, detail, message))
        self._log(guild, member, name, detail, results or ["Logged only"], message=message, dry_run=False, edited=edited, before=before)

    async def _pause_sender(self, guild: Any, member: Any, seconds: int, reason: str) -> str:
        """A few seconds' timeout on every hit breaks up a burst. It isn't a punishment, so it makes no case and leaves no mark on their record."""
        if member.is_timed_out():  # a timeout already running is longer than this, and setting a new one would cut it short
            return "Already timed out, so no pause"
        moderation = self.bot.module("moderation")
        if moderation is not None:
            moderation._expect(guild.id, member.id, "timeout")
        try:
            await member.timeout(datetime.timedelta(seconds=seconds), reason=reason)
        except discord.HTTPException as error:
            if moderation is not None:
                moderation._forget(guild.id, member.id, "timeout")
            return f"Couldn't pause them: {error.text or error.status}"
        return f"Paused them for {seconds} seconds"

    @staticmethod
    async def _delete_thread(thread: Any) -> str:
        try:
            await thread.delete()
        except discord.NotFound:
            return "The thread was already gone"
        except discord.HTTPException as error:
            return f"Couldn't delete the thread: {error.text or error.status}"
        return "Deleted the thread"

    async def _punish(self, moderation: Any, guild: Any, member: Any, action: str, reason: str, seconds: int | None, purge_days: int, dm: bool, source: str) -> str:
        length = seconds if action in ("mute", "tempban", "timeout") else None
        try:
            row = await moderation.punish(guild, member, action, moderator=None, reason=reason, seconds=length, purge_days=purge_days, dm=dm, source=source)
        except ModerationError as error:
            return f"Couldn't {action}: {error}"
        if row["extra"].get("longer_mute_stands"):
            return f"A longer mute stands, case {row['number']}"
        extra = f" for {humanize(row['seconds'])}" if row.get("seconds") else ""
        return f"{action.capitalize()}{extra}, case {row['number']}"

    async def _notice(self, message: Any, member: Any, name: str) -> str:
        delay = SPAM_NOTICE_SECONDS if name == "spam" else NOTICE_SECONDS
        try:
            await message.channel.send(f"{member.mention}, {RULE_NOTICES[name]}", delete_after=delay, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]))
        except discord.HTTPException:
            return "Couldn't post a notice in the channel"
        return "Posted a notice in the channel"

    async def _dm_offender(self, guild: Any, member: Any, name: str, heavy: str | None, seconds: int, *, keep: list[Any] | None = None) -> str:
        line = f"Automod in {guild.name}: {RULE_NOTICES[name]}"
        if heavy:
            line += f" You were {DM_RESULT[heavy]}" + (f" for {humanize(seconds)}." if seconds else ".")
        try:
            sent = await member.send(line[:messages.CONTENT_MAX], allowed_mentions=discord.AllowedMentions.none())
        except SEND_ERRORS:
            return "Couldn't DM them"
        if keep is not None:
            keep.append(sent)
        return "Sent them a DM"

    def _log_channel(self, guild: Any) -> int | None:
        """The log channel, only while it's in this server. A settings form can name any ID, and logs must not reach another server."""
        channel_id = self.cfg(guild.id)["log_channel"]
        return channel_id if channel_id and guild.get_channel_or_thread(channel_id) else None

    def _log(self, guild: Any, member: Any, name: str, detail: str, results: list[str], *, message: Any = None, dry_run: bool, edited: bool = False, before: str | None = None) -> None:
        """The entry staff read, kept for the log search too. Text past what a field holds comes as a file."""
        channel_id = self._log_channel(guild)
        logs = self.bot.module("logging")
        if not channel_id and logs is None:
            return
        place = f" in {message.channel.mention}" if message is not None else ""
        verb = "tried to edit this into a message. " if edited else ""
        embed = discord.Embed(
            title=f"Automod: {RULE_LABELS[name]}" + (" (edited in)" if edited else "") + (" (dry run)" if dry_run else ""),
            description=f"**Member:** {discord.utils.escape_markdown(name_of(member))}  {member.mention}{place}\n**Why:** {verb}{detail}\n**{'Would do' if dry_run else 'Done'}:** " + ", ".join(results),
            color=0x8D98A2 if dry_run else 0xF48942,
            timestamp=discord.utils.utcnow(),
        )
        body = self._body(message) if message is not None else ""
        if before:
            embed.add_field(name="Message before", value=before[:LOG_FIELD_MAX], inline=False)
        if body:
            embed.add_field(name="Message after" if before else "Message", value=body[:LOG_FIELD_MAX], inline=False)
        files = self._files(message) if message is not None else []
        if files:
            embed.add_field(name="Files", value=", ".join(f.filename for f in files)[:LOG_FIELD_MAX], inline=False)
            picture = next((f for f in files if (getattr(f, "content_type", None) or "").startswith("image/")), None)
            if picture is not None:
                embed.set_image(url=picture.url)
        embed.set_footer(text=f"ID: {member.id}" + (f" | Message: {message.id}" if message is not None else ""))
        whole_text = body if len(body) > LOG_FIELD_MAX else None
        attachment = discord.File(io.BytesIO(whole_text.encode("utf-8")[:LOG_FILE_MAX]), filename="message.txt") if whole_text else None
        if channel_id:
            self.bot.logs.post(channel_id, embed, file=[attachment] if attachment else None)
        if logs is not None:
            logs.keep_entry(guild, "automod", embed.to_dict(), channel_id=message.channel.id if message is not None else None, user_id=member.id, message_id=message.id if message is not None else None, search=body or detail, detail=whole_text)

    def _log_slow_pattern(self, guild: Any, pattern: str) -> None:
        channel_id = self._log_channel(guild)
        if channel_id:
            embed = discord.Embed(title="Automod: slow bad-word pattern", description=f"This pattern took too long on a message, so it didn't judge that message. Rewrite it so it can't backtrack.\n`{pattern[:1000]}`", color=0xF48942)
            self.bot.logs.post(channel_id, embed)

    # ---- the warn threshold ----

    async def warning_added(self, guild: Any, user: Any) -> None:
        """The moderation module calls this after any new warning. Past the limit, each new warning draws the punishment again."""
        cfg = self.cfg(guild.id)
        rule = cfg["warns"]
        moderation = self.bot.module("moderation")
        steps = [*([{"threshold": rule["threshold"], "punishment": rule["punishment"]}] if rule["threshold"] else []), *rule["steps"]]
        if not rule["enabled"] or not steps or moderation is None:
            return
        member = guild.get_member(user.id)
        if member is None or self._immune(member):
            return
        role_ids = {role.id for role in member.roles}
        if role_ids.intersection(cfg["allowed_roles"]) or self._rule_exempt(rule, set(), role_ids):
            return
        total = await moderation.warning_count(guild.id, member.id)
        due = max((step for step in steps if total > step["threshold"]), key=lambda step: step["threshold"], default=None)
        if due is not None:
            # Each step keeps its own repeat check, so two warnings in a row can still move someone up a step.
            await self.enforce(guild, member, "warns", {**rule, "punishment": due["punishment"]}, f"{total} warnings, over the limit of {due['threshold']}", source=THRESHOLD_SOURCE, repeat_as=f"warns:{due['threshold']}")

    # ---- the drama channel ----

    def _drama_embed(self, guild_id: int, member_name: str, user_id: int, name: str, detail: str, message: Any) -> discord.Embed:
        embed = discord.Embed(title=f"Incident: {RULE_LABELS[name]}", description=f"**Member:** {discord.utils.escape_markdown(member_name)}  <@{user_id}>\n**Why:** {detail}", color=0xF48942, timestamp=discord.utils.utcnow())
        if message is not None:
            embed.description += f"\n**Where:** {message.channel.mention}\n[Jump to where it was]({message.jump_url})"
            body = self._body(message)
            if body:
                embed.add_field(name="Message", value=body[:1000], inline=False)
            files = self._files(message)
            if files:
                embed.add_field(name="Files", value=", ".join(f.filename for f in files)[:1000], inline=False)
        embed.set_footer(text=f"ID: {user_id}")
        return embed

    async def _post_drama(self, guild: Any, member: Any, name: str, detail: str, message: Any) -> str:
        channel_id = self.cfg(guild.id)["drama_channel"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return "No drama channel is set"
        embed = self._drama_embed(guild.id, name_of(member), member.id, name, detail, message)
        try:
            sent = await channel.send(embed=embed, view=DramaView(self), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            return f"Couldn't post to the drama channel: {error.text or error.status}"
        await self.db.run(
            "INSERT INTO drama_incidents (guild_id, message_id, channel_id, user_id, user_name, rule, source_channel_id, source_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild.id, sent.id, channel.id, member.id, name_of(member), name, message.channel.id if message is not None else None, message.id if message is not None else None, time.time()),
        )
        return "Posted to the drama channel"

    def _may_handle_drama(self, interaction: Any, action: str) -> bool:
        """The same rule as running the command, so bypass roles and the bot manager role work on these buttons too."""
        member = interaction.user
        settings = self.bot.settings.get(interaction.guild.id, COMMANDS_MODULE)
        permissions = {name for name, on in member.guild_permissions if on}
        who = Who(role_ids={role.id for role in member.roles}, permissions=permissions, is_owner=interaction.guild.owner_id == member.id)
        channel = interaction.channel

        def allowed(root: str, needed: str) -> bool:
            return evaluate(settings, root=root, required=(needed,), who=who, channel_id=channel.id, category_id=getattr(channel, "category_id", None)).allowed

        if action == "dismiss":
            return any(allowed(*need) for need in DRAMA_NEEDS.values())
        return allowed(*DRAMA_NEEDS[action])

    async def drama_action(self, interaction: Any, action: str) -> None:
        guild = interaction.guild
        message = interaction.message
        if guild is None or not self._may_handle_drama(interaction, action):
            await interaction.response.send_message("You can't do that here.", ephemeral=True)
            return
        row = await self.db.one("SELECT * FROM drama_incidents WHERE guild_id = ? AND message_id = ?", (guild.id, message.id))
        if row is None:
            await interaction.response.send_message("This incident isn't on record anymore.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # Two moderators can click at once. Only the one who claims the row goes ahead.
        claimed = await self.db.run(
            "UPDATE drama_incidents SET handled_at = ?, handled_by = ?, handled_by_name = ?, outcome = ? WHERE guild_id = ? AND message_id = ? AND handled_at IS NULL",
            (time.time(), interaction.user.id, name_of(interaction.user), action, guild.id, message.id),
        )
        if not claimed:
            await interaction.followup.send(f"{discord.utils.escape_markdown(row['handled_by_name'] or 'Someone')} already handled this.", ephemeral=True)
            return
        moderation = self.bot.module("moderation")
        if action != "dismiss":
            problem = None
            if moderation is None:
                problem = "Moderation isn't loaded."
            else:
                try:
                    settings = moderation.cfg(guild.id)
                    await moderation.punish(
                        guild, guild.get_member(row["user_id"]) or Someone(row["user_id"], row["user_name"]), action, moderator=interaction.user,
                        reason=f"Automod incident: {RULE_LABELS.get(row['rule'], row['rule'])}", purge_days=settings["ban"]["days"] if action == "ban" else 0, source="drama",
                    )
                except ModerationError as error:
                    problem = str(error)
            if problem:
                await self.db.run("UPDATE drama_incidents SET handled_at = NULL, handled_by = NULL, handled_by_name = NULL, outcome = NULL WHERE guild_id = ? AND message_id = ?", (guild.id, message.id))
                await interaction.followup.send(problem, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.color = 0x53DDAC if action != "dismiss" else 0x8D98A2
        embed.description = f"{embed.description or ''}\n\n{interaction.user.mention} {DRAMA_DONE[action]}."
        try:
            await message.edit(embed=embed, view=closed_drama_view(), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            log.info("Could not update a drama post in %s: %s", guild.id, error)
        await interaction.followup.send(f"Done. {discord.utils.escape_markdown(row['user_name'])}: {DRAMA_DONE[action]}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---- command helpers ----

    async def _save(self, ctx: Any, action: str, form: dict, done: str) -> None:
        try:
            await self.bot.settings.save(ctx.guild.id, self.key, form, actor=self.bot.actor(ctx.author), action=action)
        except ValidationError as error:
            await ctx.fail(next(iter(error.fields.values()), "That wasn't accepted."))
            return
        await ctx.ok(done)

    async def _set_punishment(self, ctx: Any, name: str, words: str) -> None:
        rule = self.cfg(ctx.guild.id)[name]
        try:
            value = parse_punishment(words, keep_purge_days=rule["punishment"]["purge_days"])
        except Invalid as error:
            await ctx.fail(str(error))
            return
        if name == "warns" and value["actions"] & WARN_THRESHOLD_CANT:
            await ctx.fail(WARN_THRESHOLD_REFUSAL)
            return
        note = "" if rule["enabled"] else " The rule is still off."
        await self._save(ctx, f"automod.{name}.punishment", {name: {"punishment": value}}, f"{RULE_LABELS[name]} now does: {describe_punishment(value)}.{note}")

    async def _set_rate(self, ctx: Any, name: str, words: str) -> None:
        try:
            value = parse_rate(words, "per" if name == "spam" else "count")
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self._save(ctx, f"automod.{name}.rate", {name: {"enabled": True, "rate": value}}, f"{RULE_LABELS[name]} limit is now {value[0]} in {value[1]} seconds.")

    def _channels_from(self, guild: Any, words: str, person: Any = None) -> tuple[list[int], list[str]]:
        """Channels out of free text. With `person`, one they can't see reads the same as one that isn't there, as it does on the dashboard."""
        found: list[int] = []
        unknown: list[str] = []
        for part in split_words(words):
            match = re.fullmatch(r"<#(\d+)>|(\d{15,21})", part)
            channel = guild.get_channel(int(match.group(1) or match.group(2))) if match else discord.utils.get(guild.channels, name=part.lstrip("#"))
            if channel is None or (person is not None and not access.can_view(person, channel)):
                unknown.append(part)
            else:
                found.append(channel.id)
        return found, unknown

    def _roles_from(self, guild: Any, words: str) -> tuple[list[int], list[str]]:
        found: list[int] = []
        unknown: list[str] = []
        for part in split_words(words):
            match = re.fullmatch(r"<@&(\d+)>|(\d{15,21})", part)
            role = guild.get_role(int(match.group(1) or match.group(2))) if match else discord.utils.get(guild.roles, name=part.lstrip("@"))
            if role is None:
                unknown.append(part)
            else:
                found.append(role.id)
        return found, unknown

    @staticmethod
    def _not_found_note(unknown: list[str]) -> str:
        return f" I couldn't find: {', '.join(part.replace('`', '')[:60] for part in unknown[:5])}." if unknown else ""

    @classmethod
    def _not_found_sentence(cls, unknown: list[str], otherwise: str) -> str:
        return f"{cls._not_found_note(unknown).strip()} Nothing changed." if unknown else otherwise

    @staticmethod
    def _changed_list(current: list, picked: list, how: str) -> list:
        return list(dict.fromkeys([*current, *picked])) if how == "add" else [item for item in current if item not in picked]

    async def _switch(self, ctx: Any, name: str, on: bool) -> None:
        await self._save(ctx, f"automod.{name}.{'on' if on else 'off'}", {name: {"enabled": on}}, f"{RULE_LABELS[name]} is now {'on' if on else 'off'}.")

    async def _flip_dry_run(self, ctx: Any, name: str) -> None:
        turned_on = not self.cfg(ctx.guild.id)[name]["dry_run"]
        await self._save(ctx, f"automod.{name}.dryrun", {name: {"dry_run": turned_on}}, f"Dry run for {RULE_LABELS[name]} is now {'on, so it only logs' if turned_on else 'off'}.")

    def _rule_status(self, name: str, rule: dict) -> str:
        state = "off" if not rule["enabled"] else ("dry run" if rule["dry_run"] else "on")
        extra = ""
        if "rate" in rule:
            extra = f", {rule['rate'][0]} in {rule['rate'][1]} seconds"
        elif name == "caps":
            extra = f", {rule['threshold']}% capitals"
        elif name == "warns":
            extra = f", over {rule['threshold']} warnings"
        return f"**{RULE_LABELS[name]}:** {state}{extra}. {describe_punishment(rule['punishment']).capitalize()}."

    async def _show_rule(self, ctx: Any, name: str, *lines: str) -> None:
        rule = self.cfg(ctx.guild.id)[name]
        embed = discord.Embed(title=RULE_LABELS[name], description="\n".join((self._rule_status(name, rule), *lines))[:messages.DESCRIPTION_MAX], color=0x4286F4)
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---- dashboard ----

    def api(self, router: Any) -> None:
        router.post("/automod/import", self.api_import)
        router.post("/automod/check-pattern", self.api_check_pattern)

    async def api_check_pattern(self, req: Any) -> dict:
        """The page asks before adding a regex. The worker process compiles it, which catches what parsing can't, with no risk of a stall."""
        form = await req.json()
        pattern = text(PATTERN_MAX, minimum=1)(form.get("pattern") if isinstance(form, dict) else None)
        return {"problem": pattern_problem(pattern) or await self.regex.problem(pattern)}

    async def api_import(self, req: Any) -> dict:
        """Take in settings exported from the old setup, so moving doesn't mean retyping them."""
        body = await req.json()
        code = body.get("code") if isinstance(body, dict) else None
        try:
            data = json.loads(code) if isinstance(code, str) else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "That isn't valid JSON.")
        info = req.ops.describe_guild(req.guild_id)
        if info is None:
            raise ApiError(404, "The bot isn't in that server.")
        channel_ids = {row["id"] for row in info["channels"]}
        role_ids = {row["id"] for row in info["roles"]}
        try:
            mute_role = snowflake(data.get("muterole"))
        except Invalid:
            mute_role = None
        if mute_role in role_ids and "moderation" in req.settings.schemas:
            await req.save_settings("moderation", {"mute_role": mute_role}, action="moderation.import")
        # What the export holds replaces what's here, as the page says. A rule left in dry run from before would look switched on and do nothing.
        return await req.save_settings(self.key, _overlay(SETTINGS.defaults(), settings_from_export(data, channel_ids, role_ids)), action="automod.import")

    # ---- general commands ----

    @commands.hybrid_group(name="automod", aliases=["am"], fallback="server", description="Show this server's automod settings")
    @commands.guild_only()
    @requires("manage_guild")
    @app_commands.default_permissions(manage_guild=True)
    async def automod(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        lines = [self._rule_status(name, cfg[name]) for name in (*SCAN_ORDER, "warns")]
        log_channel, drama_channel = cfg["log_channel"], cfg["drama_channel"]
        lines.append(f"**Log channel:** {f'<#{log_channel}>' if log_channel else 'none'}")
        lines.append(f"**Drama channel:** {f'<#{drama_channel}>' if drama_channel else 'none'}")
        lines.append(f"**Media only:** {', '.join(f'<#{c}>' for c in cfg['media_channels']) or 'none'}")
        lines.append(f"**Delete unsafe files:** {'on' if cfg['delete_files'] else 'off'}")
        lines.append(f"**Allowed channels:** {', '.join(f'<#{c}>' for c in cfg['allowed_channels']) or 'none'}")
        lines.append(f"**Allowed roles:** {', '.join(f'<@&{r}>' for r in cfg['allowed_roles']) or 'none'}")
        embed = discord.Embed(title="Automod", description="\n".join(lines)[:messages.DESCRIPTION_MAX], color=0x4286F4)
        embed.set_footer(text="The bot manager role, Manage Server, Administrator, Ban Members, Kick Members and Manage Messages are always immune.")
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    def _log_refusal(self, ctx: commands.Context, field: str, channel: Any) -> str | None:
        cfg = self.cfg(ctx.guild.id)
        copying = self.reading_refusal(ctx.author, cfg, {**cfg, field: channel.id})
        return copying[1] if copying else None

    @automod.command(name="drama", description="Pick the channel where staff decide on incidents with buttons")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel")
    async def automod_drama(self, ctx: commands.Context, *, channel: discord.TextChannel) -> None:
        refused = access.post_refusal(ctx.author, channel) or self._log_refusal(ctx, "drama_channel", channel)
        if refused:
            await ctx.fail(refused)
            return
        await self._save(ctx, "automod.drama", {"drama_channel": channel.id}, f"Incidents now post in {channel.mention}.")

    @automod.command(name="log", description="Pick the channel where automod logs what it does")
    @requires("manage_guild")
    @app_commands.describe(channel="The channel")
    async def automod_log(self, ctx: commands.Context, *, channel: discord.TextChannel) -> None:
        refused = access.post_refusal(ctx.author, channel) or self._log_refusal(ctx, "log_channel", channel)
        if refused:
            await ctx.fail(refused)
            return
        await self._save(ctx, "automod.log", {"log_channel": channel.id}, f"Automod now logs to {channel.mention}.")

    @automod.command(name="media", aliases=["mo"], description="Make channels where only images and links can be posted")
    @requires("manage_guild")
    @app_commands.describe(channels="Channels, as mentions or IDs")
    async def automod_media(self, ctx: commands.Context, *, channels: str) -> None:
        found, unknown = self._channels_from(ctx.guild, channels, ctx.author)
        if not found:
            await ctx.fail(self._not_found_sentence(unknown, "Name at least one channel."))
            return
        current = self.cfg(ctx.guild.id)["media_channels"]
        await self._save(ctx, "automod.media", {"media_channels": self._changed_list(current, found, "add")}, f"Added {len(found)} media-only channels.{self._not_found_note(unknown)}")

    @automod.command(name="unmedia", aliases=["umo", "unmo"], description="Remove the media-only limit from channels")
    @requires("manage_guild")
    @app_commands.describe(channels="Channels, as mentions or IDs")
    async def automod_unmedia(self, ctx: commands.Context, *, channels: str) -> None:
        found, unknown = self._channels_from(ctx.guild, channels)
        if not found:
            await ctx.fail(self._not_found_sentence(unknown, "Name at least one channel."))
            return
        current = self.cfg(ctx.guild.id)["media_channels"]
        await self._save(ctx, "automod.unmedia", {"media_channels": self._changed_list(current, found, "remove")}, f"Removed {len(found)} media-only channels.{self._not_found_note(unknown)}")

    @automod.command(name="deletefiles", description="Turn deleting unsafe file types on or off")
    @requires("manage_guild")
    async def automod_deletefiles(self, ctx: commands.Context) -> None:
        turned_on = not self.cfg(ctx.guild.id)["delete_files"]
        await self._save(ctx, "automod.deletefiles", {"delete_files": turned_on}, f"Deleting unsafe files is now {'on' if turned_on else 'off'}.")

    @automod.command(name="threshold", aliases=["warn"], description="Set how many warnings draw a punishment. 0 turns this off")
    @requires("manage_guild")
    @app_commands.describe(limit="Warnings someone can hold before the punishment. 0 turns this off")
    async def automod_threshold(self, ctx: commands.Context, limit: int) -> None:
        none_set = " No punishment is set yet, so it only logs. Set one with `/automod warnpunish`." if not self.cfg(ctx.guild.id)["warns"]["punishment"]["actions"] else ""
        await self._save(ctx, "automod.warns.threshold", {"warns": {"enabled": limit > 0, "threshold": limit}}, f"Going over {limit} warnings now draws the punishment.{none_set}" if limit > 0 else "The warn threshold is off.")

    @automod.command(name="warnpunish", aliases=["wp", "warnpunishment"], description="Set the punishment for going over the warn threshold")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as kick, or delete, tempmute 3h")
    async def automod_warnpunish(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "warns", punishments)

    @automod.group(name="warnstep", description="More warn limits, such as a mute above three and a ban above five")
    @requires("manage_guild")
    async def automod_warnstep(self, ctx: commands.Context) -> None:
        rule = self.cfg(ctx.guild.id)["warns"]
        lines = [f"Over {step['threshold']}: {describe_punishment(step['punishment'])}" for step in rule["steps"]]
        await self._show_rule(ctx, "warns", *(lines or ["No extra steps."]))

    @automod_warnstep.command(name="add", description="Add a limit, or change the one at this number")
    @requires("manage_guild")
    @app_commands.describe(limit="Warnings someone can hold before this", punishments="Such as kick, or tempmute 3h")
    async def automod_warnstep_add(self, ctx: commands.Context, limit: int, *, punishments: str) -> None:
        rule = self.cfg(ctx.guild.id)["warns"]
        try:
            value = parse_punishment(punishments)
        except Invalid as error:
            await ctx.fail(str(error))
            return
        steps = [step for step in rule["steps"] if step["threshold"] != limit]
        await self._save(ctx, "automod.warns.steps", {"warns": {"enabled": True, "steps": [*steps, {"threshold": limit, "punishment": value}]}}, f"Going over {limit} warnings now does: {describe_punishment(value)}.")

    @automod_warnstep.command(name="remove", description="Remove the limit at this number")
    @requires("manage_guild")
    @app_commands.describe(limit="The number of warnings it was set at")
    async def automod_warnstep_remove(self, ctx: commands.Context, limit: int) -> None:
        rule = self.cfg(ctx.guild.id)["warns"]
        await self._save(ctx, "automod.warns.steps", {"warns": {"steps": [step for step in rule["steps"] if step["threshold"] != limit]}}, f"Removed the step at {limit} warnings.")

    # ---- /automod whitelist ----

    @automod.group(name="whitelist", aliases=["wl"], description="Channels and roles automod leaves alone")
    @requires("manage_guild")
    async def automod_whitelist(self, ctx: commands.Context) -> None:
        cfg = self.cfg(ctx.guild.id)
        channels = self._listed([f"<#{c}>" for c in cfg["allowed_channels"]], 1300)
        roles = self._listed([f"<@&{r}>" for r in cfg["allowed_roles"]], 500)
        await ctx.send(f"Allowed channels: {channels}\nAllowed roles: {roles}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _listed(items: list[str], room: int) -> str:
        """Comma-separated up to this many characters, saying how many more there are rather than cutting a mention in half."""
        shown: list[str] = []
        size = 0
        for index, item in enumerate(items):
            if size + len(item) + 2 > room:
                return f"{', '.join(shown)} and {len(items) - index} more"
            shown.append(item)
            size += len(item) + 2
        return ", ".join(shown) or "none"

    @automod_whitelist.command(name="channels", description="Add or remove channels automod leaves alone")
    @requires("manage_guild")
    @app_commands.describe(choice="Add or remove", channels="Channels or categories, as mentions or IDs")
    async def automod_whitelist_channels(self, ctx: commands.Context, choice: Literal["add", "remove"], *, channels: str) -> None:
        found, unknown = self._channels_from(ctx.guild, channels, ctx.author if choice == "add" else None)
        if not found:
            await ctx.fail(self._not_found_sentence(unknown, "Name at least one channel."))
            return
        current = self.cfg(ctx.guild.id)["allowed_channels"]
        await self._save(ctx, "automod.whitelist", {"allowed_channels": self._changed_list(current, found, choice)}, f"Updated the allowed channels.{self._not_found_note(unknown)}")

    @automod_whitelist.command(name="roles", description="Add or remove roles automod leaves alone")
    @requires("manage_guild")
    @app_commands.describe(choice="Add or remove", roles="Roles, as mentions, IDs or names")
    async def automod_whitelist_roles(self, ctx: commands.Context, choice: Literal["add", "remove"], *, roles: str) -> None:
        found, unknown = self._roles_from(ctx.guild, roles)
        if not found:
            await ctx.fail(self._not_found_sentence(unknown, "Name at least one role."))
            return
        current = self.cfg(ctx.guild.id)["allowed_roles"]
        await self._save(ctx, "automod.whitelist", {"allowed_roles": self._changed_list(current, found, choice)}, f"Updated the allowed roles.{self._not_found_note(unknown)}")

    async def _change_whitelist(self, ctx: commands.Context, words: str, how: str) -> None:
        """A typed list that mixes roles and channels, each by mention, ID or name."""
        channels: list[int] = []
        roles: list[int] = []
        unknown: list[str] = []
        for part in split_words(words):
            found_channels, _ = self._channels_from(ctx.guild, part, ctx.author)
            found_roles, _ = self._roles_from(ctx.guild, part)
            channels += found_channels
            roles += found_roles
            if not found_channels and not found_roles:
                unknown.append(part)
        if not channels and not roles:
            await ctx.fail(self._not_found_sentence(unknown, "Name at least one role or channel."))
            return
        cfg = self.cfg(ctx.guild.id)
        form = {"allowed_channels": self._changed_list(cfg["allowed_channels"], channels, how), "allowed_roles": self._changed_list(cfg["allowed_roles"], roles, how)}
        await self._save(ctx, "automod.whitelist", form, f"Updated what automod leaves alone: {len(channels)} channels and {len(roles)} roles.{self._not_found_note(unknown)}")

    @automod_whitelist.command(name="add", with_app_command=False, description="Have automod leave these roles and channels alone")
    @requires("manage_guild")
    async def automod_whitelist_add(self, ctx: commands.Context, *, targets: str) -> None:
        await self._change_whitelist(ctx, targets, "add")

    @automod.command(name="unwhitelist", aliases=["unwl"], with_app_command=False, description="Stop leaving these roles and channels alone")
    @requires("manage_guild")
    async def automod_unwhitelist(self, ctx: commands.Context, *, targets: str) -> None:
        await self._change_whitelist(ctx, targets, "remove")

    # ---- /automod slowmode, attachmentspam, mentionspam ----

    @automod.group(name="slowmode", description="Limit how fast a member can post")
    @requires("manage_guild")
    async def automod_slowmode(self, ctx: commands.Context) -> None:
        await self._show_rule(ctx, "spam")

    @automod_slowmode.command(name="set", description="Set the limit: 5 25 is five messages in 25 seconds")
    @requires("manage_guild")
    @app_commands.describe(rate="Messages and seconds, such as 5 25. One number N means one message every N seconds")
    async def automod_slowmode_set(self, ctx: commands.Context, *, rate: str) -> None:
        await self._set_rate(ctx, "spam", rate)

    @automod_slowmode.command(name="disable", description="Turn the posting limit off")
    @requires("manage_guild")
    async def automod_slowmode_disable(self, ctx: commands.Context) -> None:
        await self._save(ctx, "automod.spam.off", {"spam": {"enabled": False}}, "The posting limit is off.")

    @automod_slowmode.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_slowmode_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "spam")

    @automod_slowmode.command(name="punishment", aliases=["punish", "p"], description="Set what happens at the posting limit")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as delete, tempmute 20m")
    async def automod_slowmode_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "spam", punishments)

    @automod.group(name="attachmentspam", description="Limit how many files a member can post")
    @requires("manage_guild")
    async def automod_attachmentspam(self, ctx: commands.Context) -> None:
        await self._show_rule(ctx, "attachments")

    @automod_attachmentspam.command(name="set", description="Set the limit: 3 5 is three files in 5 seconds")
    @requires("manage_guild")
    @app_commands.describe(rate="Files and seconds, such as 3 5")
    async def automod_attachmentspam_set(self, ctx: commands.Context, *, rate: str) -> None:
        await self._set_rate(ctx, "attachments", rate)

    @automod_attachmentspam.command(name="disable", description="Turn the file limit off")
    @requires("manage_guild")
    async def automod_attachmentspam_disable(self, ctx: commands.Context) -> None:
        await self._save(ctx, "automod.attachments.off", {"attachments": {"enabled": False}}, "The file limit is off.")

    @automod_attachmentspam.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_attachmentspam_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "attachments")

    @automod_attachmentspam.command(name="punishment", aliases=["punish", "p"], description="Set what happens at the file limit")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as mute, defer")
    async def automod_attachmentspam_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "attachments", punishments)

    @automod.group(name="mentionspam", description="Punish members who mention too many people")
    @requires("manage_guild")
    async def automod_mentionspam(self, ctx: commands.Context) -> None:
        await self._show_rule(ctx, "mentions")

    @automod_mentionspam.command(name="set", description="Set the limit: 25 5 is 25 mentions in 5 seconds")
    @requires("manage_guild")
    @app_commands.describe(rate="Mentions and seconds, such as 25 5")
    async def automod_mentionspam_set(self, ctx: commands.Context, *, rate: str) -> None:
        await self._set_rate(ctx, "mentions", rate)

    @automod_mentionspam.command(name="disable", description="Turn the mention limit off")
    @requires("manage_guild")
    async def automod_mentionspam_disable(self, ctx: commands.Context) -> None:
        await self._save(ctx, "automod.mentions.off", {"mentions": {"enabled": False}}, "The mention limit is off.")

    @automod_mentionspam.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_mentionspam_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "mentions")

    @automod_mentionspam.command(name="punishment", aliases=["punish", "p"], description="Set what happens at the mention limit")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as tempban 24h")
    async def automod_mentionspam_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "mentions", punishments)

    # ---- /automod copyspam ----

    @automod.group(name="copyspam", description="Punish the same text posted by several accounts in one channel")
    @requires("manage_guild")
    async def automod_copyspam(self, ctx: commands.Context) -> None:
        await self._show_rule(ctx, "copies")

    @automod_copyspam.command(name="set", description="Set the limit: 5 15 is five copies in 15 seconds")
    @requires("manage_guild")
    @app_commands.describe(rate="Copies and seconds, such as 5 15")
    async def automod_copyspam_set(self, ctx: commands.Context, *, rate: str) -> None:
        await self._set_rate(ctx, "copies", rate)

    @automod_copyspam.command(name="disable", description="Turn this off")
    @requires("manage_guild")
    async def automod_copyspam_disable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "copies", False)

    @automod_copyspam.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_copyspam_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "copies")

    @automod_copyspam.command(name="punishment", aliases=["punish", "p"], description="Set what happens at the limit")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as delete, tempmute 1h")
    async def automod_copyspam_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "copies", punishments)

    # ---- /automod namefilter ----

    @automod.group(name="namefilter", description="Read names against the bad word list when someone joins or changes one")
    @requires("manage_guild")
    async def automod_namefilter(self, ctx: commands.Context) -> None:
        await self._show_rule(ctx, "names")

    @automod_namefilter.command(name="enable", description="Turn this on")
    @requires("manage_guild")
    async def automod_namefilter_enable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "names", True)

    @automod_namefilter.command(name="disable", description="Turn this off")
    @requires("manage_guild")
    async def automod_namefilter_disable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "names", False)

    @automod_namefilter.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_namefilter_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "names")

    @automod_namefilter.command(name="punishment", description="Set what happens on a bad name")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as kick, or defer")
    async def automod_namefilter_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "names", punishments)

    # ---- /automod linkspam ----

    @automod.group(name="linkspam", fallback="server", description="Show the link rules")
    @requires("manage_guild")
    async def automod_linkspam(self, ctx: commands.Context) -> None:
        rule = self.cfg(ctx.guild.id)["links"]
        await self._show_rule(ctx, "links", f"**Mode:** {LINK_MODES[rule['mode']]}", f"**Blacklist:** {', '.join(rule['blacklist'])[:1500] or 'empty'}", f"**Whitelist:** {', '.join(rule['whitelist'])[:1500] or 'empty'}")

    @automod_linkspam.command(name="rate", description="Set the limit: 1 1 punishes the first link that counts")
    @requires("manage_guild")
    @app_commands.describe(rate="Links and seconds, such as 1 1. One number N means N links in one second")
    async def automod_linkspam_rate(self, ctx: commands.Context, *, rate: str) -> None:
        await self._set_rate(ctx, "links", rate)

    @automod_linkspam.command(name="punishment", aliases=["punish", "p"], description="Set what happens at the link limit")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as delete, mute, defer")
    async def automod_linkspam_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "links", punishments)

    async def _edit_link_list(self, ctx: Any, which: str, how: str, links: str) -> None:
        rule = self.cfg(ctx.guild.id)["links"]
        try:
            picked = [domain(part) for part in split_words(links)]
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self._save(ctx, f"automod.links.{which}", {"links": {which: self._changed_list(rule[which], picked, how)}}, f"Updated the link {which}.")

    @automod_linkspam.command(name="blacklist", description="Add or remove websites on the blacklist")
    @requires("manage_guild")
    @app_commands.describe(choice="Add or remove", links="Websites, such as reddit.com twitter.com")
    async def automod_linkspam_blacklist(self, ctx: commands.Context, choice: Literal["add", "remove"], *, links: str) -> None:
        await self._edit_link_list(ctx, "blacklist", choice, links)

    @automod_linkspam.command(name="whitelist", description="Add or remove websites on the whitelist")
    @requires("manage_guild")
    @app_commands.describe(choice="Add or remove", links="Websites, such as reddit.com twitter.com")
    async def automod_linkspam_whitelist(self, ctx: commands.Context, choice: Literal["add", "remove"], *, links: str) -> None:
        await self._edit_link_list(ctx, "whitelist", choice, links)

    @automod_linkspam.command(name="clear", description="Empty the blacklist or the whitelist")
    @requires("manage_guild")
    @app_commands.describe(which="Which list to empty")
    async def automod_linkspam_clear(self, ctx: commands.Context, which: Literal["blacklist", "whitelist"]) -> None:
        await self._save(ctx, f"automod.links.{which}", {"links": {which: []}}, f"Emptied the link {which}.")

    @automod_linkspam.command(name="block", description="Punish every link not on the whitelist")
    @requires("manage_guild")
    async def automod_linkspam_block(self, ctx: commands.Context) -> None:
        await self._save(ctx, "automod.links.mode", {"links": {"enabled": True, "mode": "whitelist"}}, "Every link not on the whitelist now counts.")

    @automod_linkspam.command(name="off", description="Punish only links on the blacklist")
    @requires("manage_guild")
    async def automod_linkspam_off(self, ctx: commands.Context) -> None:
        empty = " The blacklist is empty, so no link counts. `/automod linkspam disable` turns the rule off." if not self.cfg(ctx.guild.id)["links"]["blacklist"] else ""
        await self._save(ctx, "automod.links.mode", {"links": {"mode": "blacklist"}}, f"Only links on the blacklist now count.{empty}")

    @automod_linkspam.command(name="enable", description="Turn the link rule on")
    @requires("manage_guild")
    async def automod_linkspam_enable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "links", True)

    @automod_linkspam.command(name="disable", description="Turn the link rule off")
    @requires("manage_guild")
    async def automod_linkspam_disable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "links", False)

    @automod_linkspam.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_linkspam_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "links")

    @automod_linkspam.command(name="norole", description="Punish links only from members without roles")
    @requires("manage_guild")
    async def automod_linkspam_norole(self, ctx: commands.Context) -> None:
        await self._save(ctx, "automod.links.mode", {"links": {"enabled": True, "mode": "no_role"}}, "Only links from members without roles now count.")

    # ---- /automod invitespam ----

    @automod.group(name="invitespam", fallback="server", description="Show the invite rules")
    @requires("manage_guild")
    async def automod_invitespam(self, ctx: commands.Context) -> None:
        rule = self.cfg(ctx.guild.id)["invites"]
        await self._show_rule(ctx, "invites", f"**Only members without roles:** {'yes' if rule['no_role'] else 'no'}", f"**Allowed invites:** {', '.join(rule['whitelist'])[:1500] or 'none'}", "Invites to this server never count.")

    @automod_invitespam.command(name="enable", description="Turn the invite rule on")
    @requires("manage_guild")
    async def automod_invitespam_enable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "invites", True)

    @automod_invitespam.command(name="disable", description="Turn the invite rule off")
    @requires("manage_guild")
    async def automod_invitespam_disable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "invites", False)

    @automod_invitespam.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_invitespam_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "invites")

    @automod_invitespam.command(name="punishment", aliases=["punish", "p"], description="Set what happens when someone posts an invite to another server")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as delete, warn")
    async def automod_invitespam_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "invites", punishments)

    @automod_invitespam.command(name="norole", description="Turn on or off punishing only members without roles")
    @requires("manage_guild")
    async def automod_invitespam_norole(self, ctx: commands.Context) -> None:
        turned_on = not self.cfg(ctx.guild.id)["invites"]["no_role"]
        await self._save(ctx, "automod.invites.norole", {"invites": {"no_role": turned_on}}, f"Invite punishments now reach {'only members without roles' if turned_on else 'every member'}.")

    @automod_invitespam.command(name="whitelist", description="Allow or stop allowing invites to a server")
    @requires("manage_guild")
    @app_commands.describe(choice="Add or remove", invites="Invite links, invite codes or server IDs")
    async def automod_invitespam_whitelist(self, ctx: commands.Context, choice: Literal["add", "remove"], *, invites: str) -> None:
        rule = self.cfg(ctx.guild.id)["invites"]
        try:
            picked = [invite_entry(part) for part in split_words(invites)]
        except Invalid as error:
            await ctx.fail(str(error))
            return
        await self._save(ctx, "automod.invites.whitelist", {"invites": {"whitelist": self._changed_list(rule["whitelist"], picked, choice)}}, "Updated the allowed invites.")

    # ---- /automod censor ----

    @automod.group(name="censor", fallback="server", description="Show the bad word rules")
    @requires("manage_guild")
    async def automod_censor(self, ctx: commands.Context) -> None:
        rule = self.cfg(ctx.guild.id)["words"]
        await self._show_rule(ctx, "words", f"**Entries:** {len(rule['entries'])}")

    @automod_censor.command(name="words", description="Add or remove bad words")
    @requires("manage_guild")
    @app_commands.describe(choice="Add or remove", words="Quote what has spaces. Start with word: for a whole word, regex: for a pattern")
    async def automod_censor_words(self, ctx: commands.Context, choice: Literal["add", "remove"], *, words: str) -> None:
        rule = self.cfg(ctx.guild.id)["words"]
        picked = []
        for part in split_words(words):
            kind, _, rest = part.partition(":")
            picked.append({"text": rest, "kind": kind} if rest and kind in ("word", "regex", "substring") else {"text": part, "kind": "substring"})
        if choice == "add":
            for entry in picked:
                problem = await self.regex.problem(entry["text"]) if entry["kind"] == "regex" else None
                if problem:
                    await ctx.fail(problem)
                    return
            entries = [*rule["entries"], *picked]
        else:
            gone = {(entry["text"].lower(), entry["kind"]) for entry in picked}
            entries = [entry for entry in rule["entries"] if (entry["text"].lower(), entry["kind"]) not in gone]
        form = {"enabled": True, "entries": entries} if choice == "add" else {"entries": entries}
        changed = ", ".join(entry["text"] if entry["kind"] == "substring" else f"{entry['kind']}:{entry['text']}" for entry in picked)
        await self._save(ctx, "automod.words", {"words": form}, f"{'Added' if choice == 'add' else 'Removed'} `{changed.replace('`', '')[:1500]}`. The list now holds {len(entries)} {'entry' if len(entries) == 1 else 'entries'}.")

    @automod_censor.command(name="display", description="List the bad words")
    @requires("manage_guild")
    async def automod_censor_display(self, ctx: commands.Context) -> None:
        entries = self.cfg(ctx.guild.id)["words"]["entries"]
        listed = [discord.utils.escape_markdown(entry["text"] if entry["kind"] == "substring" else f"{entry['kind']}:{entry['text']}") for entry in entries]
        shown = self._listed(listed, 1850)
        await send_private(ctx, f"||{shown}||" if listed else "The list is empty.", allowed_mentions=discord.AllowedMentions.none())

    @automod_censor.command(name="enable", description="Turn the bad word rule on")
    @requires("manage_guild")
    async def automod_censor_enable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "words", True)

    @automod_censor.command(name="disable", description="Turn the bad word rule off")
    @requires("manage_guild")
    async def automod_censor_disable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "words", False)

    @automod_censor.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_censor_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "words")

    @automod_censor.command(name="clear", description="Empty the bad word list")
    @requires("manage_guild")
    async def automod_censor_clear(self, ctx: commands.Context) -> None:
        await self._save(ctx, "automod.words", {"words": {"entries": []}}, "The list is empty.")

    @automod_censor.command(name="punishment", aliases=["punish", "p"], description="Set what happens on a bad word")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as delete, defer")
    async def automod_censor_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "words", punishments)

    # ---- /automod caps ----

    @automod.group(name="caps", description="Punish messages that are mostly capital letters")
    @requires("manage_guild")
    async def automod_caps(self, ctx: commands.Context) -> None:
        await self._show_rule(ctx, "caps")

    @automod_caps.command(name="limit", description="Set the share of capital letters that trips this")
    @requires("manage_guild")
    @app_commands.describe(percentage="From 1 to 100. Messages shorter than six characters don't count")
    async def automod_caps_limit(self, ctx: commands.Context, percentage: int) -> None:
        await self._save(ctx, "automod.caps.limit", {"caps": {"enabled": percentage > 0, "threshold": percentage}}, f"Messages at {percentage}% capitals now count." if percentage > 0 else "The caps rule is off.")

    @automod_caps.command(name="enable", description="Turn the caps rule on")
    @requires("manage_guild")
    async def automod_caps_enable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "caps", True)

    @automod_caps.command(name="disable", description="Turn the caps rule off")
    @requires("manage_guild")
    async def automod_caps_disable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "caps", False)

    @automod_caps.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_caps_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "caps")

    @automod_caps.command(name="punishment", aliases=["punish", "p"], description="Set what happens on too many capitals")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as delete, message")
    async def automod_caps_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "caps", punishments)

    # ---- /automod honeypot ----

    @automod.group(name="honeypot", description="Channels where posting at all draws a punishment, to catch spam accounts")
    @requires("manage_guild")
    async def automod_honeypot(self, ctx: commands.Context) -> None:
        await self._show_honeypot(ctx)

    async def _show_honeypot(self, ctx: Any) -> None:
        rule = self.cfg(ctx.guild.id)["honeypot"]
        await self._show_rule(ctx, "honeypot", f"**Channels:** {self._listed([f'<#{c}>' for c in rule['channels']], 3500)}")

    @automod_honeypot.command(name="server", description="Show the honeypot settings")
    @requires("manage_guild")
    async def automod_honeypot_server(self, ctx: commands.Context) -> None:
        await self._show_honeypot(ctx)

    @automod_honeypot.command(name="add", aliases=["+"], description="Make channels into honeypots")
    @requires("manage_guild")
    @app_commands.describe(channels="Channels, as mentions or IDs")
    async def automod_honeypot_add(self, ctx: commands.Context, *, channels: str) -> None:
        found, unknown = self._channels_from(ctx.guild, channels, ctx.author)
        if not found:
            await ctx.fail("Name at least one channel.")
            return
        rule = self.cfg(ctx.guild.id)["honeypot"]
        note = f" I couldn't find: {', '.join(unknown[:5])}." if unknown else ""
        await self._save(ctx, "automod.honeypot.add", {"honeypot": {"enabled": True, "channels": self._changed_list(rule["channels"], found, "add")}}, f"Added {len(found)} honeypot channels.{note}")

    @automod_honeypot.command(name="remove", aliases=["-"], description="Stop channels being honeypots")
    @requires("manage_guild")
    @app_commands.describe(channels="Channels, as mentions or IDs")
    async def automod_honeypot_remove(self, ctx: commands.Context, *, channels: str) -> None:
        found, _ = self._channels_from(ctx.guild, channels)
        rule = self.cfg(ctx.guild.id)["honeypot"]
        await self._save(ctx, "automod.honeypot.remove", {"honeypot": {"channels": self._changed_list(rule["channels"], found, "remove")}}, f"Removed {len(found)} honeypot channels.")

    @automod_honeypot.command(name="clear", description="Remove every honeypot channel")
    @requires("manage_guild")
    async def automod_honeypot_clear(self, ctx: commands.Context) -> None:
        await self._save(ctx, "automod.honeypot.clear", {"honeypot": {"channels": []}}, "No channel is a honeypot now.")

    @automod_honeypot.command(name="enable", description="Turn the honeypot on")
    @requires("manage_guild")
    async def automod_honeypot_enable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "honeypot", True)

    @automod_honeypot.command(name="disable", description="Turn the honeypot off")
    @requires("manage_guild")
    async def automod_honeypot_disable(self, ctx: commands.Context) -> None:
        await self._switch(ctx, "honeypot", False)

    @automod_honeypot.command(name="dryrun", description="Turn on or off logging what this would do, and doing nothing")
    @requires("manage_guild")
    async def automod_honeypot_dryrun(self, ctx: commands.Context) -> None:
        await self._flip_dry_run(ctx, "honeypot")

    @automod_honeypot.command(name="punishment", aliases=["punish", "p"], description="Set what happens to someone who posts in a honeypot")
    @requires("manage_guild")
    @app_commands.describe(punishments="Such as delete, defer")
    async def automod_honeypot_punishment(self, ctx: commands.Context, *, punishments: str) -> None:
        await self._set_punishment(ctx, "honeypot", punishments)


# What _punish says when nothing was done to them, which a DM must not claim.
DIDNT_LAND = ("Couldn't", "A longer mute")
DM_RESULT = {"ban": "banned", "tempban": "banned", "kick": "kicked", "timeout": "timed out", "mute": "muted"}
LINK_MODES = {"blacklist": "only blacklisted links count", "whitelist": "every link not on the whitelist counts", "no_role": "links from members without roles count"}
