"""What a person reads when a typed command can't run: one plain sentence and the command's usage, in place of the library's text."""
from __future__ import annotations

import typing
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

_ECHO_ROOM = 60
_CHILDREN_SHOWN = 30


def _echo(text: Any) -> str:
    """A person's words in a reply, cut to fit and with nothing that can break out of the code span."""
    return "`" + (" ".join(str(text or "").split()).replace("`", "")[:_ECHO_ROOM] or "that") + "`"


def _kind(converter: Any) -> str | None:
    """What a converter looks for, in plain words."""
    named = getattr(converter, "plain_kind", None)
    if named is not None or not isinstance(converter, type):
        return named
    if issubclass(converter, (discord.abc.GuildChannel, discord.Thread)):
        return "channel"
    if issubclass(converter, (discord.Member, discord.User)):
        return "member"
    if issubclass(converter, discord.Role):
        return "role"
    if issubclass(converter, (discord.Emoji, discord.PartialEmoji)):
        return "emoji"
    return None


def _kinds(annotation: Any) -> list[str]:
    """Every kind of thing a parameter looks up, with Optional and Union opened up. A number or a word isn't looked up, so it has none."""
    found: list[str] = []
    for option in typing.get_args(annotation) or (annotation,):
        if option is type(None):
            continue
        for kind in (_kinds(option) if typing.get_args(option) else [_kind(option)]):
            if kind and kind not in found:
                found.append(kind)
    return found


def _label(param: Any) -> str:
    """A parameter's name as a person would say it."""
    kinds = _kinds(param.annotation)
    return kinds[0] if len(kinds) == 1 else (param.displayed_name or param.name).replace("_", " ")


def _a(word: str) -> str:
    return f"an {word}" if word[:1].lower() in "aeiou" else f"a {word}"


def _wanted(kinds: list[str]) -> str:
    return " or ".join(_a(kind) for kind in kinds)


def usage(ctx: Any) -> str:
    """The typed form of the command that just failed, or its subcommands when it's a group that takes nothing itself."""
    command = getattr(ctx, "command", None)
    if command is None:
        return ""
    prefix = "/" if ctx.interaction is not None else (ctx.clean_prefix or "")
    signature = command.signature.replace('"', "")  # the library quotes each choice
    if isinstance(command, commands.Group) and not signature:
        names = sorted(child.name for child in command.commands)
        listed = ", ".join(f"`{name}`" for name in names[:_CHILDREN_SHOWN])
        more = f" and {len(names) - _CHILDREN_SHOWN} more" if len(names) > _CHILDREN_SHOWN else ""
        return f"`{prefix}{command.qualified_name}` takes: {listed}{more}."
    return f"Usage: `{(prefix + command.qualified_name + ' ' + signature).strip()}`"


def _too_many(ctx: Any) -> str:
    """Words left over. Either a mention or a name matched nothing, or the command has no use for them."""
    command = ctx.command
    words = ctx.view.read_rest().strip() if ctx.view is not None else ""
    first = (words.split() or [""])[0]
    given = dict(zip(command.clean_params, ctx.args[2 if command.cog is not None else 1:]))
    given.update(ctx.kwargs)
    for name, param in command.clean_params.items():
        kinds = _kinds(param.annotation)
        if kinds and given.get(name) is None:
            return f"I can't find {_wanted(kinds)} called {_echo(first)}, so nothing changed."
    if isinstance(command, commands.Group) and not command.signature:
        return f"`{command.qualified_name}` has no option called {_echo(first)}, so nothing changed."
    return f"I don't know what to do with {_echo(words)}, so nothing changed."


_NOT_FOUND = (
    (commands.MemberNotFound, "member"), (commands.UserNotFound, "member"), (commands.RoleNotFound, "role"), (commands.ChannelNotFound, "channel"),
    (commands.ThreadNotFound, "thread"), (commands.MessageNotFound, "message"), (commands.EmojiNotFound, "emoji"), (commands.GuildStickerNotFound, "sticker"),
    (commands.GuildNotFound, "server"), (commands.ScheduledEventNotFound, "event"), (commands.SoundboardSoundNotFound, "sound"),
)


def _input_sentence(ctx: Any, error: commands.UserInputError) -> str | None:
    """The one sentence for a line the library couldn't read, or None when a converter wrote its own for the person."""
    argument = getattr(ctx, "current_argument", None)
    if isinstance(error, commands.TooManyArguments):
        return _too_many(ctx)
    if isinstance(error, commands.MissingRequiredArgument):
        if typing.get_origin(error.param.annotation) is typing.Literal:
            return f"Say one of these: {', '.join(f'`{option}`' for option in typing.get_args(error.param.annotation))}."
        return f"That needs {_a(_label(error.param))}."
    if isinstance(error, commands.MissingRequiredAttachment):
        return "That needs a file attached to the message."
    if isinstance(error, commands.BadLiteralArgument):
        return f"{_echo(error.argument)} isn't one of these: {', '.join(f'`{option}`' for option in error.literals)}."
    if isinstance(error, commands.BadBoolArgument):
        return f"{_echo(error.argument)} isn't a yes or no."
    if isinstance(error, commands.BadUnionArgument):
        kinds = _kinds(error.param.annotation)
        return f"I can't find {_wanted(kinds)} called {_echo(argument)}." if kinds else f"I can't read {_echo(argument)} as the {_label(error.param)}."
    if isinstance(error, commands.ArgumentParsingError):
        return "A quotation mark in that line doesn't close. Put quotes around the whole phrase, or leave them out."
    if isinstance(error, commands.RangeError):
        low, high = error.minimum, error.maximum
        limits = f"between {low} and {high}" if low is not None and high is not None else (f"at least {low}" if low is not None else f"at most {high}")
        return f"That has to be {limits} characters long." if isinstance(error.value, str) else f"That number has to be {limits}."
    if isinstance(error, commands.BadColourArgument):
        return f"{_echo(error.argument)} isn't a color. Use a hex code such as #5865F2."
    if isinstance(error, commands.BadInviteArgument):
        return f"{_echo(error.argument)} isn't an invite, or it has expired."
    if isinstance(error, commands.ChannelNotReadable):
        return f"I can't read messages in {error.argument.mention}."
    if isinstance(error, commands.PartialEmojiConversionFailure):
        return f"{_echo(error.argument)} isn't an emoji."
    if isinstance(error, commands.BadTimestampArgument):
        return f"{_echo(error.argument)} isn't a date."
    if isinstance(error, commands.ObjectNotFound):
        return f"{_echo(error.argument)} isn't a mention or an ID."
    if isinstance(error, (commands.MissingRequiredFlag, commands.MissingFlagArgument, commands.BadFlagArgument, commands.TooManyFlags)):
        return f"I can't use what came with `{error.flag.name}`."
    for kind, word in _NOT_FOUND:
        if isinstance(error, kind):
            return f"I can't find {_a(word)} called {_echo(error.argument)}."
    if str(error).startswith("Converting to "):  # the library's words when int() or a plain function refuses a word
        param = getattr(ctx, "current_parameter", None)
        if param is None:
            return f"I can't read {_echo(argument)}."
        return f"{_echo(argument)} isn't a whole number." if int in typing.get_args(param.annotation) or param.annotation is int else f"I can't read {_echo(argument)} as the {_label(param)}."
    return None


def new_channel_name(ctx: Any, name: str, default: str) -> tuple[str, str | None]:
    """The name for a channel a command is about to make, and why it won't. Typed, a word that matched no channel is more often a typo than a wish for a new channel, so a new name says so with new: in front."""
    if getattr(ctx, "interaction", None) is not None or name == default:
        return name, None
    if name.lower().startswith("new:") and name[4:].strip():
        return name[4:].strip(), None
    if discord.utils.get(ctx.guild.text_channels, name=name) is not None:
        return name, None
    return name, f"I can't find a channel called {_echo(name)}, so nothing changed. To make a new one, write `new:{name.replace('`', '')[:60]}`."


def plain_error(ctx: Any, error: BaseException) -> str:
    """What to say when a command stops before it runs. A sentence a command or converter wrote itself comes through as written."""
    if isinstance(error, commands.ConversionError):
        return f"I can't read {_echo(getattr(ctx, 'current_argument', None))}.\n{usage(ctx)}".strip()
    if isinstance(error, commands.UserInputError):
        sentence = _input_sentence(ctx, error)
        return str(error) if sentence is None else f"{sentence}\n{usage(ctx)}".strip()
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return f"Wait {max(1, round(error.retry_after))} seconds and try again."
    if isinstance(error, commands.MaxConcurrencyReached):
        return "That's already running. Try again when it finishes."
    if isinstance(error, commands.DisabledCommand):
        return "This command is turned off."
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return "This command only works in a server."
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions)):
        return "I don't have the Discord permission to do that. Check my role and where it sits in the role list."
    if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        return "You don't have the Discord permission for this command."
    if isinstance(error, commands.NSFWChannelRequired):
        return "This command only works in an age-restricted channel."
    text = str(error)
    if not text or text.startswith(("The global check", "The check function")):
        return "You can't use this command here."
    return text
