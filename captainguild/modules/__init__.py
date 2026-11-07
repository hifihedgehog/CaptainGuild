"""Every feature, loaded in this order."""
from __future__ import annotations

from ..core.module import Module


def load_all() -> list[type[Module]]:
    from .moderation import Moderation
    from .automod import Automod
    from .logs import Logs
    from .reports import Reports
    from .voice import Voice
    from .reactionroles import ReactionRoles
    from .roles import Roles
    from .mentions import Mentions
    from .rolerules import RoleRules
    from .tags import Tags
    from .triggers import Triggers
    from .feeds import Feeds
    from .notifications import Notifications
    from .greetings import Greetings
    from .starboard import Starboard
    from .embeds import Embeds
    from .suggestions import Suggestions
    from .proposals import Proposals
    from .levels import Levels
    from .voicelink import VoiceLink
    from .sticky import Sticky
    from .autopurge import AutoPurge
    from .personalize import Personalize
    from .utilities import Utilities
    from .fun import Fun
    from .games import Games
    from .backup import Backup
    from .setup import Setup
    from .autoroles import Autoroles
    from .config import Config
    from .mydata import MyData

    return [Config, MyData, Autoroles, Moderation, Automod, Logs, Reports, Voice, ReactionRoles, Roles, RoleRules, Mentions, Tags, Triggers, Feeds, Notifications, Greetings, Starboard, Embeds, Suggestions, Proposals, Levels, VoiceLink, Sticky, AutoPurge, Personalize, Utilities, Fun, Games, Backup, Setup]
