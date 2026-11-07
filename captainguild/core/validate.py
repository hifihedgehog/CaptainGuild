"""Checks for anything a person or a browser sends.

A module describes its settings as a Schema, and every check either returns the clean value or raises
Invalid with a sentence for the person who typed it. IDs come in as text from the browser and leave here as
int, so the rest of the code only ever compares ints.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

Check = Callable[[Any], Any]

SNOWFLAKE = re.compile(r"[1-9][0-9]{14,20}")
HEX_COLOR = re.compile(r"#?[0-9a-fA-F]{6}")


class Invalid(ValueError):
    """One value was refused. The text is shown to the person, so write it for them."""


class ValidationError(Exception):
    """A form was refused. `fields` maps each field's path to the sentence explaining why."""

    def __init__(self, fields: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


def whole_number(value: Any, maximum: int = 2 ** 63 - 1) -> int | None:
    """A number or an ID a person typed, or None. Use this wherever text might be a number: `str.isdigit` also takes superscripts and other scripts,
    which `int` refuses or reads as something else, and a long run of digits overflows SQLite and costs quadratic time to read."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]{1,19}", value.strip()):
            return None
        value = int(value.strip())
    if not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def page_number(value: Any, otherwise: int, maximum: int) -> int:
    """A page or a page size from a query. A run of digits past the maximum reads as the maximum, and anything else that isn't a number from 1 up reads as the default."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value.strip()):
        return otherwise
    digits = value.strip()
    return maximum if len(digits) > 19 else min(int(digits), maximum) or otherwise


def boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise Invalid("Must be on or off.")


def integer(minimum: int | None = None, maximum: int | None = None) -> Check:
    def check(value: Any) -> int:
        if isinstance(value, bool):
            raise Invalid("Must be a whole number.")
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,18}", value.strip()):
            value = int(value.strip())
        if not isinstance(value, int):
            raise Invalid("Must be a whole number.")
        if minimum is not None and value < minimum:
            raise Invalid(f"Must be at least {minimum}.")
        if maximum is not None and value > maximum:
            raise Invalid(f"Must be at most {maximum}.")
        return value
    return check


def number(minimum: float | None = None, maximum: float | None = None) -> Check:
    def check(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:  # not-a-number passes the range checks, then can't go out as JSON
            raise Invalid("Must be a number.")
        if minimum is not None and value < minimum:
            raise Invalid(f"Must be at least {minimum}.")
        if maximum is not None and value > maximum:
            raise Invalid(f"Must be at most {maximum}.")
        try:
            out = float(value)
        except OverflowError as error:  # a whole number of hundreds of digits has no float
            raise Invalid("Must be a number.") from error
        if not math.isfinite(out):
            raise Invalid("Must be a number.")
        return out
    return check


def text(maximum: int = 2000, *, minimum: int = 0, strip: bool = True) -> Check:
    def check(value: Any) -> str:
        if not isinstance(value, str):
            raise Invalid("Must be text.")
        cleaned = value.strip() if strip else value
        if len(cleaned) < minimum:
            raise Invalid("Can't be empty." if minimum == 1 else f"Must be at least {minimum} characters.")
        if len(cleaned) > maximum:
            raise Invalid(f"Must be at most {maximum} characters. This is {len(cleaned)}.")
        return cleaned
    return check


def choice(*values: Any) -> Check:
    def check(value: Any) -> Any:
        if value not in values:
            raise Invalid("Pick one of the listed options.")
        return value
    return check


def snowflake(value: Any) -> int:
    """A Discord ID. The browser sends text, because JavaScript can't hold one as a number."""
    if isinstance(value, bool):
        raise Invalid("That isn't a Discord ID.")
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and SNOWFLAKE.fullmatch(value.strip()) and int(value.strip()) < 2 ** 63:
        return int(value.strip())
    raise Invalid("That isn't a Discord ID.")


def optional(check: Check) -> Check:
    """Empty means not set: None or an empty string both come out as None."""
    def wrapped(value: Any) -> Any:
        if value is None or value == "":
            return None
        return check(value)
    return wrapped


def list_of(check: Check, maximum: int = 500, unique: bool = True) -> Check:
    def wrapped(value: Any) -> list:
        if not isinstance(value, list):
            raise Invalid("Must be a list.")
        if len(value) > maximum:
            raise Invalid(f"Can hold at most {maximum} entries.")
        out = []
        for item in value:
            clean = check(item)
            if not unique or clean not in out:
                out.append(clean)
        return out
    return wrapped


def snowflakes(maximum: int = 500) -> Check:
    return list_of(snowflake, maximum)


def mapping_of(key: Check, value: Check, maximum: int = 500) -> Check:
    """JSON object keys are always text, so a snowflake key stays text in storage. Read it with int(key)."""
    def wrapped(data: Any) -> dict:
        if not isinstance(data, dict):
            raise Invalid("Must be a set of named values.")
        if len(data) > maximum:
            raise Invalid(f"Can hold at most {maximum} entries.")
        return {str(key(k)): value(v) for k, v in data.items()}
    return wrapped


def hex_color(value: Any) -> int:
    """A color as an int. Accepts an int or text like #ff8800."""
    if isinstance(value, bool):
        raise Invalid("That isn't a color.")
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return value
    if isinstance(value, str) and HEX_COLOR.fullmatch(value.strip()):
        return int(value.strip().lstrip("#"), 16)
    raise Invalid("That isn't a color. Use six hex digits, such as #ff8800.")


def http_url(maximum: int = 2048) -> Check:
    def check(value: Any) -> str:
        if not isinstance(value, str) or len(value) > maximum:
            raise Invalid("That isn't a usable link.")
        cleaned = value.strip()
        try:
            parts = urlsplit(cleaned)
        except ValueError as error:
            raise Invalid("That isn't a usable link.") from error
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise Invalid("Links must start with http:// or https://.")
        return cleaned
    return check


# A punishment is a set of actions plus how long the timed ones last.
DELETE, WARN, TEMPMUTE, MUTE, KICK, TEMPBAN, BAN, DRAMA, CHANNEL_MESSAGE, DM_OFFENDER, TIMEOUT = 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024
PUNISHMENT_MASK = DELETE | WARN | TEMPMUTE | MUTE | KICK | TEMPBAN | BAN | DRAMA | CHANNEL_MESSAGE | DM_OFFENDER | TIMEOUT


def punishment(value: Any) -> dict:
    """{"actions": bits, "seconds": how long tempmute, tempban and timeout last, "purge_days": message days a ban removes}."""
    if not isinstance(value, dict):
        raise Invalid("Pick at least the actions to take.")
    actions = integer(0, PUNISHMENT_MASK)(value.get("actions", 0))
    seconds = integer(0, 60 * 60 * 24 * 365 * 5)(value.get("seconds", 0))
    purge_days = integer(0, 7)(value.get("purge_days", 0))
    if actions & (TEMPMUTE | TEMPBAN | TIMEOUT) and seconds < 60:
        raise Invalid("A timed action needs a duration of at least one minute.")
    if actions & TIMEOUT and seconds > 60 * 60 * 24 * 28:
        raise Invalid("Discord limits a timeout to 28 days.")
    return {"actions": actions, "seconds": seconds, "purge_days": purge_days}


SECRET_MASK = "********"


@dataclass
class Field:
    default: Any
    check: Check | None = None
    secret: bool = False  # never sent back to a browser. Sending the mask back keeps the stored value.


class Schema:
    """Named fields with defaults. A nested Schema is a nested object."""

    def __init__(self, fields: dict[str, "Field | Schema"]):
        self.fields = fields

    def defaults(self) -> dict:
        out = {}
        for name, field in self.fields.items():
            out[name] = field.defaults() if isinstance(field, Schema) else copy.deepcopy(field.default)
        return out

    def merged(self, stored: dict | None) -> dict:
        """Stored values over today's defaults. A field added in a later version gets its default, and a removed one is dropped."""
        out = {}
        stored = stored or {}
        for name, field in self.fields.items():
            if isinstance(field, Schema):
                out[name] = field.merged(stored.get(name) if isinstance(stored.get(name), dict) else None)
            elif name in stored:
                out[name] = copy.deepcopy(stored[name])
            else:
                out[name] = copy.deepcopy(field.default)
        return out

    def clean(self, data: Any, *, base: dict | None = None, path: str = "") -> dict:
        """Check a form. Fields it leaves out keep their value from `base`, or their default. Unknown fields are refused."""
        if not isinstance(data, dict):
            raise ValidationError({path or "form": "Expected a set of fields."})
        errors: dict[str, str] = {}
        out = self.merged(base)
        for name, value in data.items():
            label = f"{path}.{name}" if path else name
            field = self.fields.get(name)
            if field is None:
                errors[label] = "Unknown setting."
                continue
            if isinstance(field, Schema):
                try:
                    out[name] = field.clean(value, base=out.get(name), path=label)
                except ValidationError as error:
                    errors.update(error.fields)
                continue
            if field.secret and isinstance(value, str) and value.startswith(SECRET_MASK):
                # A saved secret shows as the mask in the page's password box, so a click and a paste lands after it.
                value = value[len(SECRET_MASK):]
                if not value:
                    continue
            try:
                out[name] = field.check(value) if field.check else value
            except Invalid as error:
                errors[label] = str(error)
        if errors:
            raise ValidationError(errors)
        return out

    def masked(self, data: dict) -> dict:
        """A copy safe to send to a browser: a secret that's set shows as the mask, and one that isn't stays empty."""
        out = dict(data)
        for name, field in self.fields.items():
            if isinstance(field, Schema):
                if isinstance(out.get(name), dict):
                    out[name] = field.masked(out[name])
            elif field.secret and out.get(name):
                out[name] = SECRET_MASK
        return out
