"""Translation through DeepL's oneshot endpoint, the one DeepL's own browser extension calls, with the language and flag tables that
turn what people type or react with into a target language.

The anonymous endpoint takes at most 1,500 characters a request, so longer text goes in chunks split at the best boundary and is put back
together with its own spacing. Its limits are per address, so every translation the bot makes shares one pacer, and a busy answer (429) is
tried again after a growing wait.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Callable, NamedTuple

import discord

from .outside import OutsideError

FREE_URL = "https://oneshot-free.www.deepl.com/v1/translate"
PRO_URL = "https://oneshot-pro.www.deepl.com/v1/translate"
EXTENSION_ID = "cofdbpoegempjloogbagkncekinflcnj"
EXTENSION_VERSION = "1.86.0"
CHROME_MAJOR = "120"
DEFAULT_TARGET = "EN-US"
MAX_INPUT = 4000
# The anonymous endpoint refuses a request past 1,500 characters, the extension's own limit for a signed-out user.
CHUNK_LIMIT = 1450
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5  # doubled after each busy answer
LONGEST_WAIT = 30.0  # a busy answer asking for a longer wait than this ends the attempt, rather than holding a translation for minutes
PACE_SECONDS = 0.35  # between any two requests from this bot
REQUEST_SECONDS = 20.0
PER_PERSON = 5
PER_SECONDS = 30.0
FLAG_MARKS = 2000
MESSAGE_LIMIT = 2000
EMBED_LIMIT = 4096
EMBEDS_TOTAL = 6000
EMBEDS_PER_MESSAGE = 10
SERVICE_MESSAGE_LIMIT = 300

# (code, name, other names people type). The code and the name count as names too.
LANGUAGES = [
    ("AR", "Arabic", ["arabic"]),
    ("BG", "Bulgarian", ["bulgarian"]),
    ("CS", "Czech", ["czech", "cz"]),
    ("DA", "Danish", ["danish"]),
    ("DE", "German", ["german", "deutsch"]),
    ("EL", "Greek", ["greek", "gr"]),
    ("EN-US", "English (US)", ["english", "en", "american english", "us english"]),
    ("EN-GB", "English (UK)", ["british english", "uk english", "british"]),
    ("ES", "Spanish", ["spanish", "espanol", "español", "castilian"]),
    ("ES-419", "Spanish (Latin America)", ["latin american spanish", "latam spanish"]),
    ("ET", "Estonian", ["estonian"]),
    ("FI", "Finnish", ["finnish"]),
    ("FR", "French", ["french", "francais", "français"]),
    ("HE", "Hebrew", ["hebrew"]),
    ("HU", "Hungarian", ["hungarian"]),
    ("ID", "Indonesian", ["indonesian", "bahasa indonesia"]),
    ("IT", "Italian", ["italian", "italiano"]),
    ("JA", "Japanese", ["japanese", "jp", "jpn", "nihongo"]),
    ("KO", "Korean", ["korean", "kr", "hangul"]),
    ("LT", "Lithuanian", ["lithuanian"]),
    ("LV", "Latvian", ["latvian"]),
    ("NB", "Norwegian", ["norwegian", "no", "bokmal", "bokmål", "norsk"]),
    ("NL", "Dutch", ["dutch", "nederlands", "flemish"]),
    ("PL", "Polish", ["polish", "polski"]),
    ("PT-BR", "Portuguese (Brazil)", ["portuguese", "pt", "brazilian portuguese", "brazilian", "portugues", "português"]),
    ("PT-PT", "Portuguese (Portugal)", ["european portuguese", "portugal portuguese"]),
    ("RO", "Romanian", ["romanian"]),
    ("RU", "Russian", ["russian"]),
    ("SK", "Slovak", ["slovak"]),
    ("SL", "Slovenian", ["slovenian", "slovene"]),
    ("SV", "Swedish", ["swedish", "svenska"]),
    ("TR", "Turkish", ["turkish", "turkce", "türkçe"]),
    ("UK", "Ukrainian", ["ukrainian", "ua", "ukr"]),
    ("VI", "Vietnamese", ["vietnamese", "vn", "tieng viet"]),
    ("ZH", "Chinese (Simplified)", ["chinese", "zh-hans", "zh-cn", "simplified chinese", "mandarin", "cn"]),
    ("ZH-HANT", "Chinese (Traditional)", ["zh-tw", "traditional chinese", "taiwanese mandarin"]),
]

NAMES: dict[str, str] = {}  # code to name, for targets and for the source DeepL detects
ALIASES: dict[str, str] = {}  # anything typed, lowercased, to a code
for _code, _name, _extra in LANGUAGES:
    NAMES[_code] = _name
    _keys = {_code.lower(), _name.lower()} | {alias.lower() for alias in _extra}
    _keys |= {re.sub(r"[^a-z0-9]", "", key) for key in list(_keys)}
    for _key in _keys:
        if _key:
            ALIASES.setdefault(_key, _code)
for _code, _name in (("EN", "English"), ("PT", "Portuguese"), ("ZH-HANS", "Chinese (Simplified)")):
    NAMES.setdefault(_code, _name)
LONGEST_NAME = max(len(key) for key in ALIASES)

# What the endpoint takes. It wants lowercase BCP-47 codes, and an uppercase one comes back untranslated with a 200, which looks like success.
TARGET_CODES = {
    "AR": "ar", "BG": "bg", "CS": "cs", "DA": "da", "DE": "de", "EL": "el",
    "EN-GB": "en-GB", "EN-US": "en-US",
    "ES": "es", "ES-419": "es-419", "ET": "et", "FI": "fi", "FR": "fr",
    "HE": "he", "HU": "hu", "ID": "id", "IT": "it", "JA": "ja", "KO": "ko",
    "LT": "lt", "LV": "lv", "NB": "nb", "NL": "nl", "PL": "pl",
    "PT-BR": "pt-BR", "PT-PT": "pt-PT",
    "RO": "ro", "RU": "ru", "SK": "sk", "SL": "sl", "SV": "sv",
    "TR": "tr", "UK": "uk", "VI": "vi",
    "ZH": "zh-Hans", "ZH-HANS": "zh-Hans", "ZH-HANT": "zh-Hant",
    "EN": "en-US", "PT": "pt-BR",  # DeepL retired bare EN and PT as targets for the regional ones
}
SOURCE_CODES = dict(TARGET_CODES, EN="en", PT="pt")
# A source has to be a base language, so the regional targets read as their base.
SOURCE_BASE = {"EN-US": "EN", "EN-GB": "EN", "ES-419": "ES", "PT-BR": "PT", "PT-PT": "PT", "ZH-HANT": "ZH"}
SOURCE_LANGUAGES = [
    ("EN", "English"), ("ES", "Spanish"), ("PT", "Portuguese"), ("ZH", "Chinese"),
    ("AR", "Arabic"), ("BG", "Bulgarian"), ("CS", "Czech"), ("DA", "Danish"), ("DE", "German"),
    ("EL", "Greek"), ("ET", "Estonian"), ("FI", "Finnish"), ("FR", "French"), ("HE", "Hebrew"),
    ("HU", "Hungarian"), ("ID", "Indonesian"), ("IT", "Italian"), ("JA", "Japanese"), ("KO", "Korean"),
    ("LT", "Lithuanian"), ("LV", "Latvian"), ("NB", "Norwegian"), ("NL", "Dutch"), ("PL", "Polish"),
    ("RO", "Romanian"), ("RU", "Russian"), ("SK", "Slovak"), ("SL", "Slovenian"), ("SV", "Swedish"),
    ("TR", "Turkish"), ("UK", "Ukrainian"), ("VI", "Vietnamese"),
]
AUTO_WORDS = ("auto", "detect", "autodetect", "auto-detect", "auto detect")

# A Discord client's language to a target, for a one-tap translation with none named. DeepL can't translate into Croatian, Hindi or Thai,
# so those read as English.
DISCORD_LOCALES = {
    "id": "ID", "da": "DA", "de": "DE", "en-GB": "EN-GB", "en-US": "EN-US",
    "es-ES": "ES", "es-419": "ES-419", "fr": "FR", "it": "IT", "lt": "LT",
    "hu": "HU", "nl": "NL", "no": "NB", "pl": "PL", "pt-BR": "PT-BR",
    "ro": "RO", "fi": "FI", "sv-SE": "SV", "vi": "VI", "tr": "TR",
    "cs": "CS", "el": "EL", "bg": "BG", "ru": "RU", "uk": "UK",
    "zh-CN": "ZH", "ja": "JA", "zh-TW": "ZH-HANT", "ko": "KO",
}

# A country's flag, by its two-letter code, to the language a reaction with it translates into.
FLAG_LANGUAGES = {
    "US": "EN-US", "GB": "EN-GB", "AU": "EN-US", "CA": "EN-US", "NZ": "EN-US", "IE": "EN-US", "ZA": "EN-US",
    "ES": "ES",
    "MX": "ES-419", "AR": "ES-419", "CO": "ES-419", "CL": "ES-419", "PE": "ES-419", "VE": "ES-419",
    "EC": "ES-419", "GT": "ES-419", "CU": "ES-419", "BO": "ES-419", "DO": "ES-419", "UY": "ES-419",
    "PY": "ES-419", "CR": "ES-419", "PA": "ES-419", "HN": "ES-419", "SV": "ES-419", "NI": "ES-419", "PR": "ES-419",
    "FR": "FR", "BE": "FR", "LU": "FR", "MC": "FR",
    "DE": "DE", "AT": "DE", "CH": "DE", "LI": "DE",
    "IT": "IT",
    "PT": "PT-PT", "BR": "PT-BR",
    "NL": "NL",
    "RU": "RU", "BY": "RU",
    "UA": "UK",
    "JP": "JA",
    "KR": "KO",
    "CN": "ZH", "SG": "ZH",
    "TW": "ZH-HANT", "HK": "ZH-HANT", "MO": "ZH-HANT",
    "PL": "PL",
    "TR": "TR",
    "SA": "AR", "AE": "AR", "EG": "AR", "QA": "AR", "KW": "AR", "IQ": "AR", "JO": "AR", "MA": "AR",
    "DZ": "AR", "TN": "AR", "LB": "AR", "OM": "AR", "BH": "AR", "YE": "AR", "SY": "AR", "LY": "AR",
    "SE": "SV",
    "NO": "NB",
    "DK": "DA",
    "FI": "FI",
    "CZ": "CS",
    "SK": "SK",
    "SI": "SL",
    "RO": "RO", "MD": "RO",
    "HU": "HU",
    "BG": "BG",
    "GR": "EL", "CY": "EL",
    "ID": "ID",
    "VN": "VI",
    "IL": "HE",
    "EE": "ET",
    "LT": "LT",
    "LV": "LV",
}


def resolve_target(phrase: str | None) -> str | None:
    """The target a typed language name or code means, or None."""
    if not phrase:
        return None
    key = re.sub(r"\s+", " ", phrase.strip().lower())
    if key in ALIASES:
        return ALIASES[key]
    return ALIASES.get(re.sub(r"[^a-z0-9]", "", key))


def resolve_source(phrase: str | None) -> str:
    """The source a typed language means, or "auto" to let DeepL tell."""
    if not phrase or phrase.strip().lower() in AUTO_WORDS:
        return "auto"
    code = resolve_target(phrase)
    if not code:
        return "auto"
    return SOURCE_BASE.get(code, code)


def language_name(code: str | None) -> str:
    if not code:
        return "Unknown"
    upper = code.upper()
    if upper in NAMES:
        return NAMES[upper]
    return NAMES.get(upper.split("-")[0], upper)


def locale_target(locale: Any) -> str | None:
    """The target for a Discord client language, or None for one DeepL can't translate into."""
    if locale is None:
        return None
    return DISCORD_LOCALES.get(getattr(locale, "value", locale))


def flag_country(emoji: str) -> str | None:
    """The two-letter country a flag emoji shows, such as ES for the flag of Spain. None for any other emoji."""
    letters = [ord(char) for char in emoji if 0x1F1E6 <= ord(char) <= 0x1F1FF]
    if len(letters) != 2:
        return None
    return "".join(chr(letter - 0x1F1E6 + ord("A")) for letter in letters)


def flag_language(emoji: str) -> str | None:
    return FLAG_LANGUAGES.get(flag_country(emoji) or "")


# ---- what someone typed ----

_TRIGGER = re.compile(r"(?is)^\s*translate(?:[\s:,]+(.+))?$")
_SEPARATOR = re.compile(r"(?<!\s)\s+(?:to|into)\s+", re.IGNORECASE)
_LEADING_TARGET = re.compile(r"(?is)^(?:to|into)\s+(.+)$")


class Request(NamedTuple):
    """A message that starts with "translate": its text, the target it names (None for the person's own), and whether it asks for the message it replies to."""
    text: str
    target: str | None
    wants_reply: bool


def read_request(content: str, is_reply: bool) -> Request | None:
    """What a message starting with "translate" asks for, or None when it doesn't ask. A separator has to follow the word, so "translates"
    and "translate-shell" ask nothing. The last " to " or " into " followed by a language names the target, and "translate to french" alone
    or a bare "translate" in a reply asks for the message replied to."""
    found = _TRIGGER.match(content)
    if found is None:
        return None
    rest = (found.group(1) or "").strip()
    if not rest and not is_reply:
        return None
    target: str | None = None
    text = rest
    only_target = not rest
    named = False
    # Going left, each tail only grows, so the scan stops once a tail is longer than any language's name.
    for separator in reversed(list(_SEPARATOR.finditer(rest))):
        tail = rest[separator.end():].strip()
        if len(tail) > LONGEST_NAME:
            break
        code = resolve_target(tail)
        if code:
            target, text = code, rest[:separator.start()].strip()
            only_target = not text
            named = True
            break
    if not named and rest:
        leading = _LEADING_TARGET.match(rest)
        code = resolve_target(leading.group(1).strip()) if leading else None
        if code:
            target, only_target = code, True
    return Request(text, target, only_target or not text)


def message_text(message: Any) -> str:
    """The text of a message, or of the first message forwarded in it."""
    text = (getattr(message, "content", "") or "").strip()
    if text:
        return text
    for snapshot in getattr(message, "message_snapshots", None) or []:
        text = (getattr(snapshot, "content", "") or "").strip()
        if text:
            return text
    return ""


# ---- splitting long text ----

_PARAGRAPH = re.compile(r"\n{2,}")
_LINE = re.compile(r"\n+")
_SENTENCE = re.compile("[.!?…。！？][\"”’)\\]]?(\\s+)")  # Latin and CJK sentence ends, with a closing quote or bracket
_CJK_SENTENCE = re.compile("[。！？…]")
_SPACE = re.compile(r"\s+")


def _best_break(window: str) -> tuple[int, int]:
    """Where to cut: the chunk is window[:length], and the next `gap` characters of whitespace are set aside and put back after translation.
    A paragraph break wins over a line break, then a sentence end, then a space, then a hard cut, and the last of each kind wins so chunks stay large."""
    for pattern in (_PARAGRAPH, _LINE):
        best = None
        for found in pattern.finditer(window):
            if found.start() > 0:
                best = (found.start(), found.end() - found.start())
        if best:
            return best
    best = None
    for found in _SENTENCE.finditer(window):
        if found.start() > 0:
            best = (found.start(1), found.end(1) - found.start(1))
    if best:
        return best
    best = None
    for found in _CJK_SENTENCE.finditer(window):  # a CJK sentence end has no space after it
        if 0 < found.end() < len(window):
            best = (found.end(), 0)
    if best:
        return best
    best = None
    for found in _SPACE.finditer(window):
        if found.start() > 0:
            best = (found.start(), found.end() - found.start())
    if best:
        return best
    return (len(window), 0)


def chunk_text(text: str, limit: int) -> list[tuple[str, str]]:
    """(chunk, whitespace after it) pairs, each chunk at most `limit` characters. Joined back together they give the text exactly."""
    pairs = []
    index, length = 0, len(text)
    while index < length:
        if length - index <= limit:
            pairs.append((text[index:], ""))
            break
        size, gap = _best_break(text[index:index + limit])
        pairs.append((text[index:index + size], text[index + size:index + size + gap]))
        index += size + gap
    return pairs


def split_text(text: str, limit: int) -> list[str]:
    """Pieces of at most `limit` characters, cut at the best boundaries, for text too long for one embed."""
    if len(text) <= limit:
        return [text]
    pieces = []
    for chunk, _ in chunk_text(text, limit):
        chunk = chunk.strip()
        if chunk:
            pieces.append(chunk if len(chunk) <= limit else chunk[:limit - 1].rstrip() + "…")
    return pieces


def messages_for(plain: str, title: str, text: str) -> list[dict]:
    """How a translation goes out. One plain message when it fits in 2,000 characters, which keeps the simple look. Past that, embeds of up to
    4,096 characters each, as many to a message as Discord's 6,000-character total for one message's embeds allows."""
    if len(plain) <= MESSAGE_LIMIT:
        return [{"content": plain}]
    out: list[dict] = []
    embeds: list[discord.Embed] = []
    used = 0
    for index, piece in enumerate(split_text(text, EMBED_LIMIT) or [text[:EMBED_LIMIT]]):
        embed = discord.Embed(description=piece, color=discord.Color.blurple())
        size = len(piece)
        if index == 0:
            embed.title = title[:256]
            size += len(embed.title)
        if embeds and (used + size > EMBEDS_TOTAL or len(embeds) >= EMBEDS_PER_MESSAGE):
            out.append({"embeds": embeds})
            embeds, used = [], 0
        embeds.append(embed)
        used += size
    out.append({"embeds": embeds})
    return out


def error_text(result: dict) -> str:
    """What to tell someone when a translation didn't come back."""
    status = result.get("status")
    said = str(result.get("message") or "")[:SERVICE_MESSAGE_LIMIT]
    if status == 429:
        return "\U0001f310 Translation is rate-limited right now. Try again in a moment."
    if status == 503:
        return "\U0001f310 The translation service had a problem. Try again shortly."
    if status == 404:
        return "\U0001f310 There's no text to translate."
    if status == 400:
        return f"\U0001f310 {said or 'Unsupported language.'}"
    if status == 200:
        return f"\U0001f310 {said or 'Translation came back empty.'}"
    if status is None:
        return "\U0001f310 Couldn't reach the translation service. Try again shortly."
    return f"\U0001f310 Translation failed (status {status})."


# ---- limits ----

class Throttle:
    """At most PER_PERSON translations a person in PER_SECONDS, across every way of asking."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._calls: dict[int, deque] = {}

    def limited(self, user_id: int) -> bool:
        """True when this person is over the limit. Otherwise the translation counts, and False."""
        now = self.clock()
        if len(self._calls) > 10000:  # people whose window has emptied
            for stale in [user for user, calls in self._calls.items() if not calls or now - calls[-1] > PER_SECONDS]:
                del self._calls[stale]
        calls = self._calls.setdefault(user_id, deque())
        while calls and now - calls[0] > PER_SECONDS:
            calls.popleft()
        if len(calls) >= PER_PERSON:
            return True
        calls.append(now)
        return False


class FlagMarks:
    """Which flag translations are up, by message and language, so many people reacting with one flag get one translation. A mark lasts as
    long as its translation stays: the server's delete time, or for good when it keeps them. The newest FLAG_MARKS are kept."""

    def __init__(self) -> None:
        self._marks: OrderedDict = OrderedDict()

    def live(self, key: tuple, ttl: float | None, now: float) -> bool:
        posted = self._marks.get(key)
        if posted is None:
            return False
        return ttl is None or now - posted < ttl

    def mark(self, key: tuple, now: float) -> None:
        self._marks[key] = now
        self._marks.move_to_end(key)
        while len(self._marks) > FLAG_MARKS:
            self._marks.popitem(last=False)

    def unmark(self, key: tuple) -> None:
        self._marks.pop(key, None)


# ---- the service ----

class Translator:
    def __init__(self, outside: Any, *, sleep: Callable[[float], Any] = asyncio.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.outside = outside
        self.sleep = sleep
        self.clock = clock
        self.pace = PACE_SECONDS
        self._gate = asyncio.Lock()
        self._last = 0.0
        self.instance = str(uuid.uuid4())  # the extension keeps one for good, so the bot keeps one for its run

    async def _wait_turn(self) -> None:
        async with self._gate:
            wait = self.pace - (self.clock() - self._last)
            if wait > 0:
                await self.sleep(wait)
            self._last = self.clock()

    @staticmethod
    def headers(token: str | None) -> dict:
        """The headers the extension sends. "Authorization: None" is the literal text it sends signed out, and leaving the header off takes another path."""
        return {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Authorization": f"Bearer {token}" if token else "None",
            "Origin": f"chrome-extension://{EXTENSION_ID}",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "User-Agent": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36",
        }

    async def _one(self, text: str, target: str, source: str, token: str | None) -> dict:
        """One chunk, already short enough. A busy answer is tried again after a growing wait, and anything else comes back at once."""
        code = TARGET_CODES.get((target or "").upper())
        if not code:
            return {"ok": False, "status": 400, "message": f"Unsupported language: {target}."}
        payload: dict[str, Any] = {
            "text": [text], "target_lang": code, "usage_type": "Translate",
            "app_information": {"os": "brex_macOS", "os_version": f"brex_chrome_{CHROME_MAJOR}.0.0.0", "app_version": EXTENSION_VERSION,
                                "app_build": "chrome_web_store", "instance_id": self.instance},
        }
        if source and source.lower() != "auto":
            from_code = SOURCE_CODES.get(source.upper())
            if from_code:
                payload["source_lang"] = from_code
        url = PRO_URL if token else FREE_URL
        body = json.dumps(payload).encode()
        result: dict = {"ok": False, "status": None, "message": "The translation never ran."}
        for attempt in range(MAX_RETRIES + 1):
            await self._wait_turn()
            try:
                reply = await self.outside.post(url, data=body, headers=self.headers(token), timeout=REQUEST_SECONDS, max_bytes=1024 * 1024)
            except OutsideError as error:
                return {"ok": False, "status": None, "message": str(error)}
            try:
                answer = reply.json()
            except OutsideError:
                answer = None
            if reply.status == 200 and isinstance(answer, dict):
                items = answer.get("translations") or []
                first = items[0] if items and isinstance(items[0], dict) else {}
                translated = first.get("text")
                if isinstance(translated, str) and translated:
                    detected = str(first.get("detected_source_language") or "").upper()
                    return {"ok": True, "text": translated, "source_lang": detected or "auto", "target_lang": target.upper()}
                return {"ok": False, "status": 200, "message": "Translation came back empty."}
            said = answer.get("message") if isinstance(answer, dict) else None
            result = {"ok": False, "status": reply.status, "message": said if isinstance(said, str) else None}
            if reply.status != 429 or attempt == MAX_RETRIES:
                return result
            wait = max(BACKOFF_SECONDS * 2 ** attempt + random.uniform(0, 0.4), reply.retry_after(0.0))
            if wait > LONGEST_WAIT:
                return result
            await self.sleep(wait)
        return result

    async def translate(self, text: str, target: str, source: str = "auto", *, token: str | None = None) -> dict:
        """{"ok": True, "text", "source_lang", "target_lang"}, or {"ok": False, "status", "message"}. Text past MAX_INPUT is cut there. Each chunk
        of a long text goes alone, and the first chunk that fails ends the translation."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "status": 404, "message": "No text to translate."}
        text = text[:MAX_INPUT]
        pieces = chunk_text(text, CHUNK_LIMIT)
        if len(pieces) == 1:
            return await self._one(pieces[0][0], target, source, token)
        out: list[str] = []
        detected, final = None, target.upper()
        for chunk, gap in pieces:
            if chunk.strip():
                result = await self._one(chunk, target, source, token)
                if not result["ok"]:
                    return result
                out.append(result["text"])
                detected = detected or result["source_lang"]
                final = result["target_lang"]
            else:
                out.append(chunk)
            out.append(gap)
        return {"ok": True, "text": "".join(out).strip(), "source_lang": detected or "auto", "target_lang": final}
