"""run_tag: a template's output and everything its blocks asked of Discord.

The order is fixed. The require and blacklist blocks come first, and a refusal stops everything else. Then automod reads what a member's template wrote. Then the output goes out, then its reactions, then the command
blocks, and the invocation is deleted last, since a command may still read the message it came from.
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext.commands.view import StringView

from .. import access, messages
from ..context import Context
from ..perms import answer_place
from ..shortcuts import expand_line
from . import budget
from .budget import OverBudget, budgeted_render
from .engine import Actions, Scope
from .payload import render_strict
from .text import OUT_OF_TIME, LimitReached

log = logging.getLogger(__name__)

MAX_PINGED = 100  # the most users Discord takes in one message's allowed mentions
NO_MENTIONS = discord.AllowedMentions.none()
WARNING_EMOJI = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"
TOO_MUCH = "This template does too much work, so it was stopped."
TOO_BUSY = "Tags have done too much work this minute. Try again in a moment."
BENCHED = "This template ran out of time just now, so it's off for a few minutes."
OWNER_ONLY = "This tag's commands only run for its owner until the server owner or an Administrator trusts it."
NOT_TRUSTED = "This trigger's commands don't run until the server owner or an Administrator trusts it."
# What a command block may not reach. A tag running a tag could loop, and setting up a reaction role needs a person there to answer its prompts.
BLOCKED_COMMANDS = frozenset({"tag", "reactionrole"})

_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_BARE_ID = re.compile(r"\d{15,21}")
_MEMBER_MENTION = re.compile(r"<@!?(\d{15,21})>")
_EMOJI_NAME = re.compile(r":([\w~+-]+):")

# The names people type most, for a reaction written as text on the dashboard. In Discord the client turns these into the emoji before the bot sees them.
_COMMON_EMOJI = {
    "+1": "\N{THUMBS UP SIGN}", "thumbsup": "\N{THUMBS UP SIGN}", "-1": "\N{THUMBS DOWN SIGN}", "thumbsdown": "\N{THUMBS DOWN SIGN}",
    "joy": "\N{FACE WITH TEARS OF JOY}", "sob": "\N{LOUDLY CRYING FACE}", "laughing": "\N{SMILING FACE WITH OPEN MOUTH AND TIGHTLY-CLOSED EYES}",
    "smile": "\N{SMILING FACE WITH OPEN MOUTH AND SMILING EYES}", "grinning": "\N{GRINNING FACE}", "thinking": "\N{THINKING FACE}",
    "heart": "\N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}", "fire": "\N{FIRE}", "tada": "\N{PARTY POPPER}", "eyes": "\N{EYES}",
    "ok_hand": "\N{OK HAND SIGN}", "clap": "\N{CLAPPING HANDS SIGN}", "wave": "\N{WAVING HAND SIGN}", "star": "\N{WHITE MEDIUM STAR}",
    "100": "\N{HUNDRED POINTS SYMBOL}", "white_check_mark": "\N{WHITE HEAVY CHECK MARK}", "x": "\N{CROSS MARK}", "warning": WARNING_EMOJI,
}

@dataclass
class TagRun:
    """What came of a run_tag call. Nothing here is an error for the caller to handle: anything the person should know has been told to them."""
    ran: bool = False
    sent: Any = None
    refused: bool = False
    too_big: bool = False
    problem: str | None = None   # what stopped it or went wrong, for a trigger, which has no one to tell
    benched: bool = False        # this run is what switched the template off for everyone, so whoever owns it should hear


class BlockContext(Context):
    """The context a command block's command sees. Its replies follow the tag's redirect and {silent}, and its errors always show."""
    silent: bool = False
    as_moderator: bool = False
    destination: Any = None

    async def send(self, content: Any = None, **kwargs: Any) -> Any:
        kwargs.pop("ephemeral", None)
        decision = self.decision
        if self.silent or (decision and decision.muted):
            return None
        place = self.destination if self.destination is not None else self.channel
        # A block runs the same command a person could type, so a Restricted command's answer belongs in the bot channel here too.
        wanted = (decision.redirect or decision.restricted_channel) if decision else None
        if wanted:
            found = self.author if wanted == "dm" else answer_place(self.guild, wanted)
            place = found or place
        return await place.send(content, **kwargs)

    async def reply(self, content: Any = None, **kwargs: Any) -> Any:
        return await self.send(content, **kwargs)

    async def fail(self, text: str, **kwargs: Any) -> Any:
        kwargs.pop("ephemeral", None)
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        return await self.channel.send(f"\N{CROSS MARK} {text}", **kwargs)


def find_channel(guild: Any, text: str) -> Any:
    """A channel by mention, ID or name, in this server only."""
    cleaned = text.strip()
    match = _CHANNEL_MENTION.fullmatch(cleaned)
    number = match.group(1) if match else (cleaned if _BARE_ID.fullmatch(cleaned) else None)
    if number is not None:
        found = guild.get_channel_or_thread(int(number))
        return found if hasattr(found, "send") else None
    name = cleaned.lstrip("#")
    return next((c for c in guild.channels if c.name == name and hasattr(c, "send")), None)


def _places(channel: Any) -> set[int]:
    """A channel counts as itself, its parent when it's a thread, and its category."""
    places = {channel.id}
    parent = getattr(channel, "parent", None)
    if parent is not None:
        places.add(parent.id)
    source = parent if parent is not None else channel
    category = getattr(source, "category_id", None)
    if category:
        places.add(category)
    return places


def _entry_matches(entry: str, member: Any, channel: Any) -> bool:
    roles = list(getattr(member, "roles", []))
    match = _ROLE_MENTION.fullmatch(entry) or _CHANNEL_MENTION.fullmatch(entry)
    number = match.group(1) if match else (entry if entry.isdigit() else None)
    if number is not None:
        return int(number) in {role.id for role in roles} | _places(channel)
    wanted = entry.lstrip("#").casefold()
    return any(role.name.casefold() == wanted for role in roles) or str(getattr(channel, "name", "")).casefold() == wanted


def refusal_from(actions: Actions, member: Any, channel: Any) -> tuple[bool, str | None]:
    """Whether a require or blacklist block stops this person here, and the message it wants shown."""
    for message, entries in actions.requires:
        if not any(_entry_matches(entry, member, channel) for entry in entries):
            return True, message or None
    for message, entries in actions.blacklists:
        if any(_entry_matches(entry, member, channel) for entry in entries):
            return True, message or None
    return False, None


def _emoji_text(guild: Any, token: str) -> str:
    match = _EMOJI_NAME.fullmatch(token)
    if match is None:
        return token
    name = match.group(1)
    found = next((e for e in getattr(guild, "emojis", []) if e.name == name), None)
    return str(found) if found is not None else _COMMON_EMOJI.get(name, token)


async def _react_all(message: Any, guild: Any, tokens: list[str]) -> None:
    for token in tokens:
        try:
            await message.add_reaction(_emoji_text(guild, token))
        except (discord.HTTPException, TypeError, ValueError) as error:
            log.info("A tag's reaction %r was refused: %s", token, error)


def send_refusal(error: discord.HTTPException) -> str:
    """What to do about a post Discord turned down, by why it did."""
    if isinstance(error, discord.Forbidden):
        return "The bot can't post that here. It needs View Channel, Send Messages and Embed Links."
    if error.status >= 500:
        return "Discord didn't take this message. Try again."
    return "Discord refused this message. Check its links and embeds."


async def _tell(ctx: Any, channel: Any, text: str, outcome: TagRun) -> None:
    """A problem the person who ran it should hear about. Triggers have no one to tell, so theirs reach the log and `outcome.problem`, which the page shows."""
    outcome.problem = text
    if ctx is None:
        log.info("Template result in channel %s: %s", getattr(channel, "id", None), text)
        return
    try:
        await ctx.fail(text)
    except discord.HTTPException:
        pass


def _synthetic_message(bot: Any, channel: Any, author: Any) -> Any:
    """A role trigger has no message behind it, but a command needs one to run from. Discord's library builds its slash commands the same way."""
    data = {
        "id": discord.utils.time_snowflake(discord.utils.utcnow()), "reactions": [], "embeds": [], "mention_everyone": False, "tts": False, "pinned": False,
        "edited_timestamp": None, "type": discord.MessageType.default.value, "flags": 0, "content": "", "mentions": [], "mention_roles": [], "attachments": [],
    }
    message = discord.Message(state=channel._state, channel=channel, data=data)
    message.author = author
    return message


def _with_full_name(bot: Any, text: str) -> str:
    """Typed text falls through to the short names when no command answers, so a block's text does too, in the same order."""
    stripped = text.lstrip()
    line = expand_line(bot, stripped)
    return text if line == stripped else line


async def _run_command(bot: Any, text: str, *, admin: bool, override: bool, silent: bool, source: Any, ctx: Any, channel: Any, author: Any, destination: Any) -> None:
    guild = channel.guild
    message = copy.copy(source) if source is not None else _synthetic_message(bot, channel, author)
    text = _with_full_name(bot, text)
    message.content = text
    message.author = guild.me if admin else author
    view = StringView(text)
    prefixes = bot.settings.get(guild.id, "core")["prefixes"]
    prefix = getattr(ctx, "prefix", None) or (prefixes[0] if prefixes else "")
    run_ctx = BlockContext(prefix=prefix, view=view, bot=bot, message=message)
    run_ctx.silent = silent
    run_ctx.as_moderator = override or admin
    run_ctx.destination = destination
    view.skip_ws()
    invoker = view.get_word()
    run_ctx.invoked_with = invoker
    run_ctx.command = bot.all_commands.get(invoker)
    if run_ctx.command is None:
        await run_ctx.fail(f"There is no command called `{invoker}`.")
        return
    root = run_ctx.command.root_parent or run_ctx.command
    if root.qualified_name in BLOCKED_COMMANDS:
        await run_ctx.fail(f"A tag can't run `{root.qualified_name}`.")
        return
    await bot.invoke(run_ctx)


async def run_tag(
    bot: Any, payload: dict, scope: Scope, *, ctx: Any = None, message: Any = None, owner_id: int | None = None, trusted: bool = False, staff_wrote: bool = False,
    written_by: int | None = None, label: str | None = None,
) -> TagRun:
    """Run a stored message for real.

    `ctx` answers a command, tag or slash alike, and without it the output goes to scope.channel. `message` is the message that set it off, when there is one.
    `trusted` says the server owner or an Administrator vouched for this template. Without it, command blocks only run for the person who owns it, at their own permissions, so a
    template can't borrow a moderator's by getting them to run it. {cmdAdmin} then counts as a plain command block and {override} does nothing.
    `staff_wrote` says someone with Manage Server wrote it, as with a trigger. Its output can ping the person running it, the person they named, and anyone its own text mentions, and its command blocks still need `trusted`. Text a member typed chooses nobody.
    Anything else pings nobody but the person it answers in the bot channel, and automod reads what it wrote before it goes out. Unless it's trusted, {redirect} needs the owner, or `written_by` for a template with no owner, to be able to post in the channel.
    `label` names it in the activity log. When the budget is spent it comes back with `too_big` set and nothing done. Never raises.
    """
    outcome = TagRun()
    channel = scope.channel
    person = scope.acting_member if scope.acting_member is not None else scope.member
    template = budget.template_key(getattr(scope.guild, "id", 0), payload)
    try:
        await _run(bot, payload, scope, outcome, template, ctx=ctx, message=message, owner_id=owner_id, trusted=trusted, staff_wrote=staff_wrote, written_by=written_by, label=label)
    except LimitReached as error:
        outcome.too_big = True
        log.info("A template in channel %s hit a limit: %s", getattr(channel, "id", None), error)
        if str(error) == OUT_OF_TIME:
            person_id = getattr(person, "id", None)
            outcome.benched = budget.bench(template, str(scope.args or ""), person_id, is_owner=not trusted and owner_id is not None and person_id == owner_id)
        await _tell(ctx, channel, TOO_MUCH, outcome)
    except discord.HTTPException as error:
        log.info("Discord refused a template's message in channel %s: %s", getattr(channel, "id", None), error)
        await _tell(ctx, channel, send_refusal(error), outcome)
    except Exception:
        log.exception("A template failed in channel %s", getattr(channel, "id", None))
        await _tell(ctx, channel, "Something went wrong running that. It's been logged.", outcome)
    finally:
        await _settle_interaction(ctx)
    return outcome


async def _settle_interaction(ctx: Any) -> None:
    """A slash command has to be answered, even when a tag's output went elsewhere or there was none."""
    interaction = getattr(ctx, "interaction", None)
    if interaction is None or interaction.response.is_done():
        return
    try:
        await interaction.response.send_message("Done.", ephemeral=True)
    except discord.HTTPException:
        pass


def _strings(value: Any) -> list[str]:
    """Every piece of text in a message, links and hidden fields alike."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _pingable(payload: dict, scope: Scope, person: Any) -> discord.AllowedMentions:
    """The people a template written by staff can ping: whoever it's about, and whoever its author named. Text a member typed can't add anyone."""
    wanted = [getattr(someone, "id", None) for someone in (person, scope.member, scope.target)]
    wanted += [int(number) for text in _strings(payload) for number in _MEMBER_MENTION.findall(text)]
    users = list(dict.fromkeys(number for number in wanted if number is not None))[:MAX_PINGED]
    return discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(number) for number in users], replied_user=False)


def _answers_in_bot_channel(ctx: Any) -> bool:
    decision = getattr(ctx, "decision", None)
    return bool(decision and decision.restricted_channel and not decision.redirect)


async def _automod_problem(bot: Any, person: Any, rendered: dict) -> str | None:
    """Automod never scans the bot's own posts, and blocks can build at run time what a check at saving couldn't see, so a member's template meets the rules here."""
    automod = bot.module("automod")
    if automod is None or person is None:
        return None
    return await automod.output_problem(person, "\n".join(_strings(rendered)))


async def _run(
    bot: Any, payload: dict, scope: Scope, outcome: TagRun, template: tuple, *, ctx: Any, message: Any, owner_id: int | None, trusted: bool, staff_wrote: bool, written_by: int | None, label: str | None,
) -> None:
    channel, guild = scope.channel, scope.guild
    member = scope.acting_member if scope.acting_member is not None else scope.member
    is_slash = getattr(ctx, "interaction", None) is not None
    invocation = message if message is not None else (ctx.message if ctx is not None and not is_slash else None)

    guild_id = getattr(guild, "id", 0)
    if budget.is_benched(template, str(scope.args or "")):
        outcome.too_big = True
        await _tell(ctx, channel, BENCHED, outcome)
        return
    try:
        # A run can take a quarter second of steady work, and on the event loop that would hold up every server.
        rendered, actions, _ = await budgeted_render(
            lambda: render_strict(payload, scope), guild_id, pool=budget.STAFF if trusted or staff_wrote else budget.MEMBERS, member_id=getattr(member, "id", None),
        )
    except OverBudget:
        outcome.too_big = True
        outcome.problem = TOO_BUSY
        log.info("Template time is spent for this minute in server %s", guild_id)
        if is_slash:
            await ctx.fail(TOO_BUSY)
        elif ctx is not None and invocation is not None:
            await _react_all(invocation, guild, [WARNING_EMOJI])
        return

    refused, reason = refusal_from(actions, member, channel)
    if refused:
        outcome.refused = True
        if reason:
            await _answer(ctx, channel, reason)
        elif invocation is not None:
            await _react_all(invocation, guild, [WARNING_EMOJI])
        elif is_slash:
            await ctx.fail("You can't use this here.")
        return

    if not trusted and not staff_wrote:
        problem = await _automod_problem(bot, member, rendered)
        if problem:
            outcome.refused = True
            await _tell(ctx, channel, problem, outcome)
            return

    destination = await _destination(actions, ctx, scope, owner_id if owner_id is not None else written_by, trusted, outcome)
    if destination is False:
        return
    outcome.ran = True

    if messages.has_content(rendered):
        if trusted or staff_wrote:
            mentions = _pingable(payload, scope, member)
        elif destination is None and _answers_in_bot_channel(ctx):  # it moved to another channel, so they have to hear where it went
            mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(member.id)], replied_user=False)
        else:
            mentions = NO_MENTIONS
        kwargs = messages.send_kwargs(rendered, allowed_mentions=mentions)
        if destination is not None:
            try:
                outcome.sent = await destination.send(**kwargs)
            except discord.Forbidden:
                await _tell(ctx, channel, "I can't send that there. It could be closed DMs or a missing channel permission.", outcome)
        elif ctx is not None:
            outcome.sent = await ctx.send(**kwargs)
        else:
            outcome.sent = await channel.send(**kwargs)
    if outcome.sent is not None:
        await _react_all(outcome.sent, guild, actions.react)
    if invocation is not None and not actions.delete:
        await _react_all(invocation, guild, actions.react_up)

    source = message if message is not None else (ctx.message if ctx is not None else None)
    may_run = trusted or (owner_id is not None and getattr(member, "id", None) == owner_id)
    if actions.commands and not may_run:
        await _tell(ctx, channel, NOT_TRUSTED if staff_wrote else OWNER_ONLY, outcome)
    for text, wants_admin in actions.commands if may_run else ():
        if wants_admin and trusted:  # the command acts as the bot, so its case and the audit log name nobody
            await bot.activity_log.record(guild.id, bot.actor(member), "tag.command", detail=f"{label or 'A template'}: {text}"[:500])
        try:
            await _run_command(
                bot, text, admin=wants_admin and trusted, override=actions.override and trusted, silent=actions.silent, source=source, ctx=ctx,
                channel=channel, author=member, destination=destination,
            )
        except discord.HTTPException as error:
            log.info("A command block's reply was refused: %s", error)

    if actions.delete and invocation is not None:
        try:
            await invocation.delete()
        except discord.HTTPException:
            pass


async def _answer(ctx: Any, channel: Any, text: str) -> None:
    """A require or blacklist block's own message. It's the author's text, shown to one person, so nothing in it pings."""
    none = discord.AllowedMentions.none()
    if ctx is not None:
        await ctx.send(text, allowed_mentions=none, ephemeral=True)
    else:
        await channel.send(text, allowed_mentions=none)


async def _destination(actions: Actions, ctx: Any, scope: Scope, writer_id: int | None, trusted: bool, outcome: TagRun) -> Any:
    """Where the output goes when it isn't the usual place: a member, a channel, None for the usual place, or False when it can't go anywhere."""
    if actions.destination == "dm":
        return scope.acting_member if scope.acting_member is not None else scope.member
    if actions.destination != "channel":
        return None
    guild, channel = scope.guild, scope.channel
    place = find_channel(guild, actions.channel_text or "")
    if place is None:
        await _tell(ctx, channel, "This sends its output to a channel that doesn't exist.", outcome)
        return False
    if not trusted:
        writer = guild.get_member(writer_id) if writer_id else None
        if writer is None or not access.can_post(writer, place):
            await _tell(ctx, channel, "Whoever wrote this can't post in the channel it sends its output to.", outcome)
            return False
    return place


__all__ = ["BlockContext", "TagRun", "find_channel", "refusal_from", "run_tag"]
