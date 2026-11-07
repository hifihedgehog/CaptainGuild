"""The base class every feature builds on.

A module is a discord.py Cog with a few more class attributes:

    key          the name of its settings and its page, such as "autoroles"
    category     the tab its commands appear under on the Commands page
    schema       its settings as a Schema, or None
    migrations   its tables, as a list of migration steps (see db.py)
    message_migrations   the same for tables in the separate message database, for high-volume rows

and these hooks:

    async on_load()   runs after the database is ready and before the gateway connects. Register timers and persistent views here.
    def api(router)   registers the module's dashboard endpoints.
    roles_given, role_refusal, staff_roles, power_roles, posted_channels, pinged_channels, reading_refusal   tell the dashboard what a form's settings do, so it can refuse what the person saving couldn't do themselves.

and these for restoring a backup: role_timers, child_links and row_limits, described below, and before_restore, on_guild_restored and after_restore.
"""
from __future__ import annotations

from typing import Any, Callable, ClassVar, Sequence

from discord.ext import commands

from .validate import Schema


class Module(commands.Cog):
    key: ClassVar[str] = ""
    category: ClassVar[str] = "Misc"
    schema: ClassVar[Schema | None] = None
    migrations: ClassVar[Sequence[Any]] = ()
    message_migrations: ClassVar[Sequence[Any]] = ()
    # Timers that must not run late from a backup, such as one that changes someone's roles or lifts a ban. One a backup holds that's already past due either ran back then or is out of date, so a restore leaves it out.
    role_timers: ClassVar[Sequence[str]] = ()
    # Rows that carry their own guild_id and hang off a row in another table with no foreign key, as {table: {column: "parent_table.parent_column"}}. A restore refuses a row whose parent isn't in this server, keeps it with its parent as one unit, and follows the parent when it takes a new number. tests.test_backup fails when a table like this isn't named here.
    child_links: ClassVar[dict[str, dict[str, str]]] = {}

    # The most rows a server can have in a table, as the save paths enforce them. A restore holds to the same numbers, since a file passes through none of those paths and one server's rows can slow a feature for all of them.
    row_limits: ClassVar[dict[str, int]] = {}

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.db = bot.db
        self.message_db = bot.message_db

    def cfg(self, guild_id: int) -> dict:
        """This module's settings for a server. Read-only: change them with `save`."""
        return self.bot.settings.get(guild_id, self.key)

    async def on_load(self) -> None:
        pass

    async def before_restore(self, guild_id: int) -> None:
        """A backup's rows are about to replace this server's. Save what memory holds, and change nothing for this server until `after_restore`."""

    async def on_guild_restored(self, guild_id: int) -> None:
        """A backup was just put back for this server. Reload anything this module keeps in memory from its tables."""

    async def after_restore(self, guild_id: int) -> None:
        """The restore that `before_restore` announced is over, whether it worked or not."""

    def transient_roles(self, guild_id: int) -> set[int]:
        """Roles this feature holds on someone for a limited time or under its own rule, such as a mute or a birthday role. Sticky roles never give these back to someone who returns, because nothing would remain to take them off."""
        return set()

    def roles_given(self, settings: dict) -> dict[str, set[int]]:
        """The roles these settings hand to members, by field. The dashboard stops someone adding one at or above their own highest role, which would be a way to give it to themselves. Where a row says who gets the role, the field carries that after a colon, as in "rewards:5", so moving the role to easier terms counts as adding it."""
        return {}

    def role_refusal(self, guild: Any, role: Any) -> str | None:
        """Why a form can't newly name this role, beyond the rank checks every feature shares. The dashboard answers with this sentence, so a form refuses what the feature's command refuses."""
        return None

    def staff_roles(self, settings: dict) -> dict[str, set[int]]:
        """The roles these settings make count as staff, by field. Only the owner or an Administrator changes one, and @everyone is refused."""
        return {}

    def power_roles(self, settings: dict) -> dict[str, set[int]]:
        """Other roles these settings give a power to, such as the role an alert pings, by field. Anyone with the page can change one, and @everyone is refused."""
        return {}

    def posted_channels(self, settings: dict) -> dict[str, set[int]]:
        """The channels where these settings post text a person wrote, by field. The dashboard stops someone newly naming one they couldn't post in themselves."""
        return {}

    def pinged_channels(self, settings: dict) -> dict[str, set[int]]:
        """The channels where these settings ping @everyone, @here or a role that can't be mentioned, by field. The dashboard stops someone newly naming one where they couldn't send that ping themselves."""
        return {}

    def reading_refusal(self, member: Any, before: dict, after: dict) -> tuple[str, str] | None:
        """The field and the sentence when this change would copy what's said in channels this person can't read to a place they can. Commands ask here too."""
        return None

    def api(self, router: Any) -> None:
        pass


def requires(*permissions: str) -> Callable:
    """The Discord permission a command normally needs, such as requires("ban_members"). Goes on the command function.

    The server can grant bypass roles for it on the Commands page, so moderation commands stay visible in Discord's
    command list and the check runs when they're used.
    """
    def decorator(func: Any) -> Any:
        target = getattr(func, "callback", func)
        target.__required_permissions__ = permissions
        return func
    return decorator


def required_permissions(command: Any) -> tuple[str, ...]:
    return getattr(command.callback, "__required_permissions__", ())


def staff_only(label: str) -> Callable:
    """Who a command lets in through a check in its own body, such as staff_only("Moderators"). It changes nothing about who can run it. The docs and the Commands page print it, because they can't read the body."""
    def decorator(func: Any) -> Any:
        target = getattr(func, "callback", func)
        target.__staff_only__ = label
        return func
    return decorator


def staff_only_label(command: Any) -> str | None:
    return getattr(command.callback, "__staff_only__", None)
