"""Short names for text commands, and where each command lives. Typed text only, so Discord's slash list stays organized in groups."""
from __future__ import annotations

import itertools
import re
from typing import Any

_WORD = re.compile(r"\S+")
_MOST_STEPS = 4  # a short name can lead to another, and no chain in the table runs longer

SHORTCUTS = {
    # server rules
    "cleanconfig": "config cleanconfig", "disable": "config disable", "enable": "config enable", "ignore": "config ignore", "unignore": "config unignore", "modonly": "config modonly", "unmodonly": "config unmodonly",
    "restrict": "config restrict", "unrestrict": "config unrestrict", "modrole": "config modrole", "config modrole clear": "config clearmodrole", "diagnose": "config diagnose",
    # automod
    "attachmentspam": "automod attachmentspam", "capslimit": "automod caps limit", "capspunish": "automod caps punishment", "capsp": "automod caps punishment", "capspunishment": "automod caps punishment",
    "censor": "automod censor", "deletefiles": "automod deletefiles", "honeypot": "automod honeypot", "invitespam": "automod invitespam", "linkspam": "automod linkspam", "mentionspam": "automod mentionspam",
    "slowmode": "automod slowmode", "sm": "automod slowmode",
    "automod linkspam bl": "automod linkspam blacklist add", "automod linkspam wl": "automod linkspam whitelist add", "automod linkspam unbl": "automod linkspam blacklist remove", "automod linkspam unblacklist": "automod linkspam blacklist remove", "automod linkspam unwl": "automod linkspam whitelist remove",
    "automod linkspam clearbl": "automod linkspam clear blacklist", "automod linkspam clearwl": "automod linkspam clear whitelist",
    "automod censor add": "automod censor words add", "automod censor remove": "automod censor words remove", "automod censor list": "automod censor display",
    # feeds
    "announce": "feeds announce", "autofeeds": "feeds autofeeds", "autofeed": "feeds autofeeds", "af": "feeds autofeeds", "feed": "feeds",
    # fun
    "8ball": "fun 8ball", "addemoji": "fun addemoji", "aesthetics": "fun aesthetics", "boldfancy": "fun boldfancy", "boldfraktur": "fun boldfraktur", "choose": "fun choose", "clap": "fun clap",
    "ae": "fun aesthetics", "bf": "fun boldfancy", "choice": "fun pick", "coin": "fun coinflip", "ds": "fun double", "ff": "fun fancy", "flip": "fun coinflip", "pick": "fun pick", "sc": "fun smallcaps", "select": "fun pick", "steal": "fun addemoji", "ud": "fun urbandictionary",
    "coinflip": "fun coinflip", "double": "fun double", "emojify": "fun emojify", "fancy": "fun fancy", "fraktur": "fun fraktur", "owofy": "fun owofy", "roll": "fun roll", "smallcaps": "fun smallcaps",
    "space": "fun space", "urbandictionary": "fun urbandictionary", "cat": "cute cat", "catbomb": "cute cat true", "dog": "cute dog", "dogbomb": "cute dog true", "aww": "cute aww", "awwbomb": "cute aww true",
    "shake": "fun shake",
    # games
    "incursion": "games incursion", "assault": "games incursion", "assaults": "games incursion", "invasion": "games invasion", "invasions": "games invasion", "whenisthereset": "games reset",
    "pickmyclass": "games pickmyclass", "pickmyspec": "games pickmyspec", "fortnite": "games fortnite", "fn": "games fortnite", "roblox": "games roblox", "roblox play": "games roblox",
    # greetings
    "banmessage": "greetings banmessage", "banmsg": "greetings banmessage", "farewell": "greetings farewell", "leave": "greetings farewell", "greet": "greetings welcome", "welcome": "greetings welcome",
    "joindm": "greetings joindm", "dmjoin": "greetings joindm", "pmjoin": "greetings joindm", "joinpm": "greetings joindm", "testgreet": "greetings testgreet",
    # levels and the starboard
    "levels": "level", "star": "starboard", "level leaderboard": "leaderboard",
    # members, the server and the bot
    "i": "members info", "info": "members info", "newmembers": "members newusers", "newusers": "members newusers", "oldest": "members oldest", "oldmembers": "members oldusers", "oldusers": "members oldusers", "youngest": "members youngest",
    "botpermissions": "misc botpermissions", "charinfo": "misc charinfo", "permissions": "misc permissions",
    "dump": "stats dump", "serverinfo": "stats serverinfo", "uptime": "stats uptime",
    # suggestions
    "approve": "suggestion approve", "consider": "suggestion consider", "deny": "suggestion deny", "implemented": "suggestion implemented", "suggest": "suggestion suggest", "suggestions": "suggestion",
    # tags and reminders
    "commands": "tag list", "search": "tag search", "taglist": "tag list", "reminders": "reminder mine", "rm": "reminder", "subscribe": "reminder subscribe", "timer": "reminder",
    # triggers, sticky messages and alerts
    "ar": "triggers", "autoresponse": "triggers", "stickylist": "sticky", "stickyremove": "sticky remove", "stickytemplate": "sticky fromtemplate", "yt": "youtube", "twitch fmt": "twitch format",
    "games alerts enable": "games alerts free-on", "games alerts disable": "games alerts free-off",
    # one word for a channel that several kinds of message share
    "set welcome": "greetings set_welcome", "set farewell": "greetings set_farewell", "set twitch": "twitch default_channel",
    # levels: spellings people type from other bots' documentation
    "lvl": "level", "lvls": "level", "level set_message_xp reset": "level set_message_xp default", "level bl": "level blacklist add", "level unbl": "level blacklist remove",
    "level unblacklist": "level blacklist remove", "level resetall": "level reset server", "level shh": "level toggle announcements", "level dm": "level toggle dm", "level replace": "level toggle replace",
    "level color": "level config color", "level colour": "level config color", "level accent": "level config accent", "level opacity": "level config opacity", "level alpha": "level config opacity",
    # voice: the slash names are single words, and the spaced spellings of them keep working when typed
    "vc report": "vc_report", "vc mute": "vc_mute", "vc deafen": "vc_deafen", "vc disconnect": "vc_disconnect", "vc release": "vc_release",
    "vc access grant": "vc_access_grant", "vc access revoke": "vc_access_revoke", "vc access status": "vc_access_status", "mini_mute apply": "mini_mute", "mini_mute lift": "mini_mute_lift",
    # slash names that are single words, with the spaced spellings of them kept for typing
    "proposal submit": "submit_proposal", "fun_roles deploy": "deploy_fun_roles", "fun_roles delete": "delete_fun_roles",
    # embeds
    "cembed": "embed custom", "customembed": "embed custom", "ecembed": "embed editcustom", "editembed": "embed edit", "embedsource": "embed source", "embedunhide": "embed unhide",
}

# Names that reach a command only when no tag holds them, because a tag with that name is common. Nothing reserves these for the bot.
TAG_FIRST_SHORTCUTS = {"invite": "misc invite", "reset": "games reset", "wiki": "misc wiki"}

# A group named here takes a line with no subcommand word in it as this subcommand. `slowmode 5 25` means `slowmode set 5 25`.
BARE_FORMS = {
    "automod slowmode": "automod slowmode set", "automod attachmentspam": "automod attachmentspam set", "automod mentionspam": "automod mentionspam set", "automod copyspam": "automod copyspam set",
    "automod linkspam": "automod linkspam rate", "automod censor": "automod censor words add", "automod whitelist": "automod whitelist add",
    "games fortnite": "games fortnite pc", "giveaway": "giveaway create", "level blacklist": "level blacklist add", "log": "log config", "starboard": "starboard setup", "sticky": "sticky add", "triggers ignore": "triggers ignore add", "twitch": "twitch add", "youtube": "youtube add",
}

# A command whose first word picks one of these, and which reads any other first word as the named form. `level reset @member` means `level reset member @member`.
FIRST_WORD_FORMS = {
    "level reset": ({"member", "server", "all"}, "level reset member"),
    "level set_message_xp": ({"set", "default", "reset"}, "level set_message_xp set"),
    "starboard emoji": ({"set", "reset"}, "starboard emoji set"),
}


def _one_step(bot: Any, line: str, *, forms: bool = True) -> tuple[str, str]:
    """One rewrite of a line: what the words read so far come to, and the rest of the line as typed. Both are empty when nothing changes."""
    found = list(itertools.islice(_WORD.finditer(line), 8))
    if not found:
        return "", ""
    words = [match.group() for match in found]
    lowered = [word.lower() for word in words]
    command = bot.all_commands.get(words[0])
    if command is None:
        for count in (3, 2, 1):
            key = " ".join(lowered[:count])
            target = (SHORTCUTS.get(key) or TAG_FIRST_SHORTCUTS.get(key)) if len(words) >= count else None
            if target is not None:
                return target, line[found[count - 1].end():]
        return "", ""
    index = 1
    respelled = False
    while True:
        for count in (2, 1):
            key = " ".join([command.qualified_name, *lowered[index:index + count]])
            if index + count <= len(words) and key in SHORTCUTS:
                return SHORTCUTS[key], line[found[index + count - 1].end():]
        children = getattr(command, "all_commands", None)
        if children is None or index >= len(words):
            break
        child = children.get(words[index])
        if child is None:
            child = children.get(lowered[index])
            respelled = respelled or child is not None
        if child is None:
            break
        command = child
        index += 1
    if forms and index < len(words):
        if lowered[index] == getattr(command, "fallback", None):  # the word a group's own function goes by on the slash side
            if index == len(words) - 1:
                return command.qualified_name, ""
            # With more after it, that word can't stand for the group's own function when the rest would read as a bare form or a subcommand. The group answers for itself.
            if command.qualified_name in BARE_FORMS or lowered[index + 1] in command.all_commands:
                return "", ""
            return command.qualified_name, line[found[index].end():]
        if command.qualified_name in BARE_FORMS:
            return BARE_FORMS[command.qualified_name], line[found[index - 1].end():]
        choices, form = FIRST_WORD_FORMS.get(command.qualified_name, ((), ""))
        if form and lowered[index] not in choices:
            return form, line[found[index - 1].end():]
    return (command.qualified_name, line[found[index - 1].end():]) if respelled else ("", "")


def expand_line(bot: Any, line: str) -> str:
    """A typed line, with what comes after the prefix spelled the long way: short names read, and a group's bare form given its subcommand. The rest of the line goes along as typed. A line with nothing to change comes back the same."""
    for _ in range(_MOST_STEPS):
        target, rest = _one_step(bot, line)
        if not target:
            return line
        line = target + rest
    return line


def named_command(bot: Any, name: str) -> tuple[Any, str | None] | None:
    """The command these words name, for a rule or a help page: short names read, subcommands followed. Comes with its name in the server's rules, which is None for a top-level command, since a rule on one of those goes under its own name. None when the words name nothing."""
    typed = name.lower().split()
    if not typed:
        return None
    line = " ".join(typed)
    typed_left = len(typed)  # how many words at the end of the line are still the person's own, since a short name can bring a value such as `cute cat`
    for _ in range(_MOST_STEPS):
        target, rest = _one_step(bot, line, forms=False)
        if not target:
            break
        line = target + rest
        typed_left = min(typed_left, len(rest.split()))
    words = line.split()
    command = bot.all_commands.get(words[0])
    if command is None:
        return None
    if command.extras.get("rules_of"):  # the same command under a typed name, so a rule goes on the name it answers to
        words = [*command.extras["rules_of"].split(), *words[1:]]
        command = bot.all_commands.get(words[0])
    for index, word in enumerate(words[1:], 1):
        children = getattr(command, "all_commands", None)
        child = children.get(word) if children is not None else None
        if child is None:
            fallback = getattr(command, "fallback", None)
            if word == fallback and index == len(words) - 1:
                return command, f"{command.qualified_name} {fallback}"
            if typed_left:
                return None
            break
        command = child
    return command, command.qualified_name if command.parent else None
