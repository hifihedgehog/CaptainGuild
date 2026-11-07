import asyncio
import json
import time
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core import messages
from captainguild.core.ops import DiscordOps, OpsError
from captainguild.core.validate import Invalid
from captainguild.modules import reactionroles as module
from captainguild.modules.logs import Logs
from captainguild.modules.reactionroles import RR, Option, ReactionRoles, RoleButton, RoleMenu, clean_emoji, emoji_key, key_of, parse_message_ref, parse_pairs, split_text_and_pairs
from tests.fakes import BotTest, FakeChannel, FakeMember, _FakeResponse, new_id
from tests.gateway import GatewayTest
from tests.gateway import new_id as gateway_id

CHECK = "\N{WHITE HEAVY CHECK MARK}"
RED = "\N{LARGE RED CIRCLE}"
BLUE = "\N{LARGE BLUE CIRCLE}"
GREEN = "\N{LARGE GREEN CIRCLE}"


class Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})


class Response:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def defer(self, **kwargs) -> None:
        self.calls.append(("defer", kwargs))

    async def edit_message(self, **kwargs) -> None:
        self.calls.append(("edit", kwargs))


def interaction(guild, member, message_id) -> SimpleNamespace:
    return SimpleNamespace(guild_id=guild.id, user=member, message=SimpleNamespace(id=message_id), response=Response(), followup=Followup())


class Base(BotTest):
    modules = [ReactionRoles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rr = self.cog(ReactionRoles)
        self.ops = self.bot.ops
        self.ops.channels = [self._row(self.channel)]
        self.ops.channel = lambda guild_id, channel_id: next((c for c in self.ops.channels if c["id"] == channel_id), None) if guild_id == self.guild.id else None
        self.ops.describe_guild = lambda guild_id: self.info() if guild_id == self.guild.id else None
        self.ops.member = lambda guild_id, user_id: None
        self.red = self.guild.role("Red", 3)
        self.blue = self.guild.role("Blue", 3)
        self.green = self.guild.role("Green", 3)
        self.verified = self.guild.role("Verified", 4)

    async def asyncTearDown(self) -> None:
        await self.rr.cog_unload()
        await super().asyncTearDown()

    def _row(self, channel: FakeChannel) -> dict:
        return {"id": channel.id, "name": channel.name, "type": 0, "parent_id": None, "position": 0, "nsfw": False}

    def info(self) -> dict:
        return {
            "id": self.guild.id, "name": self.guild.name, "icon": None, "member_count": len(self.guild.members), "owner_id": self.guild.owner_id,
            "channels": self.ops.channels,
            "roles": [{"id": r.id, "name": r.name, "color": 0, "position": r.position, "managed": r.managed, "default": r.is_default(), "permissions": [n for n, on in r.permissions if on], "members": 0} for r in self.guild.roles],
            "emojis": [], "me": {"id": self.me.id, "name": "Bot", "nick": None, "avatar": "", "top_role_position": self.me.top_role.position, "permissions": ["administrator"]},
        }

    async def make(self, *rows, foreign: bool = False, mine: bool = True, fresh: bool = True, **fields) -> RR:
        """A message with reaction roles. Each row is (emoji, role) or (emoji, [roles])."""
        rr = RR(0, self.guild.id, self.channel.id, foreign=foreign, created_at=time.time(), **fields)
        for slot, (emoji, roles) in enumerate(rows):
            roles = roles if isinstance(roles, list) else [roles]
            rr.options.append(Option(slot, emoji, emoji_key(emoji), [r.id for r in roles]))
        sent = await self.ops.send_message(self.channel.id, {"content": "pick", "embeds": [], "buttons": []}, components=self.rr.render(rr))
        rr.message_id = sent["id"]
        if foreign:
            self.ops.messages[sent["id"]]["mine"] = False
        await self.rr._save(rr, None, fresh=fresh)
        return rr

    def reacted(self, rr: RR, emoji: str, member: FakeMember) -> SimpleNamespace:
        """A reaction landing on Discord, then its gateway event."""
        self.ops.react_as(rr.message_id, emoji, member.id, bot=member.bot)
        return SimpleNamespace(guild_id=self.guild.id, channel_id=rr.channel_id, message_id=rr.message_id, user_id=member.id, emoji=discord.PartialEmoji(name=emoji), member=member)

    def unreacted(self, rr: RR, emoji: str, member: FakeMember) -> SimpleNamespace:
        stored = self.ops.messages.get(rr.message_id) or {}
        row = next((r for r in stored.get("reactions", []) if r["emoji"].replace("\ufe0f", "") == emoji.replace("\ufe0f", "")), None)
        if row is not None:
            people = stored.get("reactors", {}).get(row["emoji"], [])
            if any(p["id"] == member.id for p in people):
                people[:] = [p for p in people if p["id"] != member.id]
                row["count"] -= 1
        return SimpleNamespace(guild_id=self.guild.id, channel_id=rr.channel_id, message_id=rr.message_id, user_id=member.id, emoji=discord.PartialEmoji(name=emoji), member=None)

    async def add(self, rr: RR, emoji: str, member: FakeMember) -> None:
        await self.rr.on_raw_reaction_add(self.reacted(rr, emoji, member))
        await self.rr.settle()

    async def remove(self, rr: RR, emoji: str, member: FakeMember) -> None:
        await self.rr.on_raw_reaction_remove(self.unreacted(rr, emoji, member))
        await self.rr.settle()

    def reactors(self, rr: RR, emoji: str) -> list[int]:
        stored = self.ops.messages[rr.message_id]
        row = next((r for r in stored.get("reactions", []) if r["emoji"] == emoji), None)
        return [p["id"] for p in stored.get("reactors", {}).get(emoji, [])] if row else []

    def newcomer(self, *roles) -> FakeMember:
        return FakeMember(self.guild, "newcomer", *roles)

    async def click(self, rr: RR, member: FakeMember, *slots: int, view=None) -> str:
        event = interaction(self.guild, member, rr.message_id)
        await self.rr.clicked(event, list(slots), view=view)
        return event.followup.sent[-1]["content"]


class TypesByReactionTest(Base):
    async def test_verify_gives_the_role_clears_the_reaction_and_never_takes_it_back(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        await self.add(rr, CHECK, member)
        self.assertIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [])
        self.assertIsNone(await self.db.scalar("SELECT 1 FROM rr_seen WHERE message_id = ?", (rr.message_id,)))
        await self.remove(rr, CHECK, member)
        self.assertIn(self.verified, member.roles)

    async def test_verify_clicked_again_changes_nothing_but_still_clears_the_reaction(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer(self.verified)
        await self.add(rr, CHECK, member)
        self.assertEqual([c for c in member.calls if c[0] == "add_roles"], [])
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_normal_gives_and_takes(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.assertIn(self.red, member.roles)
        self.assertEqual(self.reactors(rr, RED), [member.id])
        await self.remove(rr, RED, member)
        self.assertNotIn(self.red, member.roles)
        self.assertIsNone(await self.db.scalar("SELECT 1 FROM rr_seen WHERE message_id = ?", (rr.message_id,)))

    async def test_one_emoji_can_hand_out_several_roles(self):
        rr = await self.make((RED, [self.red, self.blue]))
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.assertIn(self.red, member.roles)
        self.assertIn(self.blue, member.roles)

    async def test_unique_swaps_and_takes_the_old_reaction_off_without_the_echo_costing_a_role(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), kind="unique")
        member = self.newcomer()
        await self.add(rr, RED, member)
        await self.add(rr, BLUE, member)
        self.assertNotIn(self.red, member.roles)
        self.assertIn(self.blue, member.roles)
        self.assertEqual(self.reactors(rr, RED), [])
        # Discord reports the bot's own removal like any other.
        echo = SimpleNamespace(guild_id=self.guild.id, channel_id=rr.channel_id, message_id=rr.message_id, user_id=member.id, emoji=discord.PartialEmoji(name=RED), member=None)
        await self.rr.on_raw_reaction_remove(echo)
        await self.rr.settle()
        self.assertIn(self.blue, member.roles)
        # A real removal of the new reaction still works, because the echo was spent.
        await self.remove(rr, BLUE, member)
        self.assertNotIn(self.blue, member.roles)

    async def test_drop_only_takes(self):
        rr = await self.make((RED, self.red), kind="drop")
        holder = self.newcomer(self.red)
        await self.add(rr, RED, holder)
        self.assertNotIn(self.red, holder.roles)
        self.assertEqual(self.reactors(rr, RED), [])
        other = self.newcomer()
        await self.add(rr, RED, other)
        self.assertNotIn(self.red, other.roles)

    async def test_reversed_takes_on_reacting_and_gives_on_unreacting(self):
        rr = await self.make((RED, self.red), kind="reversed")
        member = self.newcomer(self.red)
        await self.add(rr, RED, member)
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(self.reactors(rr, RED), [member.id])
        await self.remove(rr, RED, member)
        self.assertIn(self.red, member.roles)

    async def test_limit_refuses_past_the_limit_and_takes_the_reaction_off(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), (GREEN, self.green), kind="limit", limit=2)
        member = self.newcomer()
        await self.add(rr, RED, member)
        await self.add(rr, BLUE, member)
        await self.add(rr, GREEN, member)
        self.assertNotIn(self.green, member.roles)
        self.assertEqual(self.reactors(rr, GREEN), [])
        await self.remove(rr, RED, member)
        await self.add(rr, GREEN, member)
        self.assertIn(self.green, member.roles)

    async def test_binding_keeps_the_first_choice(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), kind="binding")
        member = self.newcomer()
        await self.add(rr, RED, member)
        await self.add(rr, BLUE, member)
        self.assertIn(self.red, member.roles)
        self.assertNotIn(self.blue, member.roles)
        self.assertEqual(self.reactors(rr, BLUE), [])
        await self.remove(rr, RED, member)
        self.assertIn(self.red, member.roles)

    async def test_lock_hands_out_nothing(self):
        rr = await self.make((RED, self.red), kind="lock")
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(self.reactors(rr, RED), [])

    async def test_temp_ends_on_its_timer_and_lets_the_member_pick_again(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=600)
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.assertIn(self.red, member.roles)
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 601), 1)
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(self.reactors(rr, RED), [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (rr.message_id,)), 0)

    async def test_temp_taken_off_early_cancels_the_timer(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=600)
        member = self.newcomer()
        await self.add(rr, RED, member)
        await self.remove(rr, RED, member)
        self.assertIsNone(await self.bot.timers.get_key("rr_temp", f"{self.guild.id}:{member.id}:{self.red.id}"))

    async def test_a_reaction_with_another_emoji_does_nothing(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, BLUE, member)
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(self.reactors(rr, BLUE), [member.id])

    async def test_a_reaction_on_an_unrelated_message_does_nothing(self):
        member = self.newcomer()
        event = SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=new_id(), user_id=member.id, emoji=discord.PartialEmoji(name=RED), member=member)
        await self.rr.on_raw_reaction_add(event)
        await self.rr.settle()
        self.assertEqual(member.calls, [])

    async def test_bots_get_nothing_but_their_reaction_goes_on_record(self):
        rr = await self.make((RED, self.red))
        other_bot = FakeMember(self.guild, "other bot", bot=True)
        await self.add(rr, RED, other_bot)
        self.assertNotIn(self.red, other_bot.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (rr.message_id,)), 1)

    async def test_the_variation_selector_doesnt_split_an_emoji(self):
        heart = "\N{HEAVY BLACK HEART}\ufe0f"
        rr = await self.make((heart, self.red))
        member = self.newcomer()
        event = SimpleNamespace(guild_id=self.guild.id, channel_id=rr.channel_id, message_id=rr.message_id, user_id=member.id, emoji=discord.PartialEmoji(name="\N{HEAVY BLACK HEART}"), member=member)
        await self.rr.on_raw_reaction_add(event)
        await self.rr.settle()
        self.assertIn(self.red, member.roles)

    async def test_a_custom_emoji_matches_by_id(self):
        emoji_id = new_id()
        rr = await self.make((f"<:party:{emoji_id}>", self.red))
        member = self.newcomer()
        event = SimpleNamespace(guild_id=self.guild.id, channel_id=rr.channel_id, message_id=rr.message_id, user_id=member.id, emoji=discord.PartialEmoji(name="renamed", id=emoji_id), member=member)
        await self.rr.on_raw_reaction_add(event)
        await self.rr.settle()
        self.assertIn(self.red, member.roles)


class ListsAndLimitsTest(Base):
    async def test_whitelist(self):
        rr = await self.make((RED, self.red), whitelist=[self.verified.id])
        outsider = self.newcomer()
        await self.add(rr, RED, outsider)
        self.assertNotIn(self.red, outsider.roles)
        self.assertEqual(self.reactors(rr, RED), [])
        insider = self.newcomer(self.verified)
        await self.add(rr, RED, insider)
        self.assertIn(self.red, insider.roles)

    async def test_blacklist(self):
        rr = await self.make((RED, self.red), blacklist=[self.role_muted.id])
        muted = self.newcomer(self.role_muted)
        await self.add(rr, RED, muted)
        self.assertNotIn(self.red, muted.roles)
        self.assertEqual(self.reactors(rr, RED), [])

    async def test_a_whitelist_whose_role_was_deleted_stays_closed(self):
        rr = await self.make((RED, self.red), whitelist=[new_id()])
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.assertNotIn(self.red, member.roles)

    async def test_maximum_holders_counts_who_holds_the_role(self):
        rr = await self.make((RED, self.red))
        rr.options[0].limits[self.red.id] = 1
        await self.rr._write(rr)
        self.newcomer(self.red)
        latecomer = self.newcomer()
        await self.add(rr, RED, latecomer)
        self.assertNotIn(self.red, latecomer.roles)
        self.assertEqual(self.reactors(rr, RED), [])

    async def test_a_role_above_the_bot_is_refused(self):
        high = self.guild.role("High", 20)
        rr = await self.make((RED, high))
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.assertNotIn(high, member.roles)
        self.assertEqual(self.reactors(rr, RED), [member.id])

    async def test_a_dangerous_role_needs_the_server_setting(self):
        strong = self.guild.role("Strong", 3, "ban_members")
        rr = await self.make((RED, strong))
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.assertNotIn(strong, member.roles)
        await self.bot.settings.save(self.guild.id, "commands", {"elevated_roles": True})
        await self.add(rr, RED, member)
        self.assertIn(strong, member.roles)

    async def test_linked_messages_swap_across_messages(self):
        first = await self.make((RED, self.red))
        second = await self.make((BLUE, self.blue))
        await self.rr._link_group(self.guild.id, [first, second])
        member = self.newcomer()
        await self.add(first, RED, member)
        await self.add(second, BLUE, member)
        self.assertNotIn(self.red, member.roles)
        self.assertIn(self.blue, member.roles)
        self.assertEqual(self.reactors(first, RED), [])

    async def test_linked_binding_counts_every_message(self):
        first = await self.make((RED, self.red), kind="binding")
        second = await self.make((BLUE, self.blue), kind="binding")
        await self.rr._link_group(self.guild.id, [first, second])
        member = self.newcomer()
        await self.add(first, RED, member)
        await self.add(second, BLUE, member)
        self.assertIn(self.red, member.roles)
        self.assertNotIn(self.blue, member.roles)


class QueueTest(Base):
    async def test_a_members_reactions_run_in_order_however_slow_discord_is(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), (GREEN, self.green), kind="unique")
        member = self.newcomer()
        order = []
        real_add, real_remove = member.add_roles, member.remove_roles

        async def slow_add(*roles, **kwargs):
            order.append(("add", roles[0].name, "start"))
            await asyncio.sleep(0.02 if roles[0] is self.red else 0)
            order.append(("add", roles[0].name, "end"))
            await real_add(*roles, **kwargs)

        async def slow_remove(*roles, **kwargs):
            order.append(("remove", roles[0].name, "start"))
            await real_remove(*roles, **kwargs)

        member.add_roles, member.remove_roles = slow_add, slow_remove
        for emoji in (RED, BLUE, GREEN):
            await self.rr.on_raw_reaction_add(self.reacted(rr, emoji, member))
        await self.rr.settle()
        self.assertEqual(order[:2], [("add", "Red", "start"), ("add", "Red", "end")])
        self.assertEqual(order[-2:], [("add", "Green", "start"), ("add", "Green", "end")])
        self.assertIn(self.green, member.roles)
        self.assertNotIn(self.red, member.roles)
        self.assertNotIn(self.blue, member.roles)

    async def test_the_queue_knows_what_it_just_changed_before_the_gateway_says_so(self):
        """The cached member is stale right after a change, as it is on Discord until the member-update event arrives."""
        rr = await self.make((RED, self.red), (BLUE, self.blue), kind="unique")
        member = self.newcomer()
        real_add = member.add_roles
        added = []

        async def add_without_updating_cache(*roles, **kwargs):
            added.extend(roles)

        member.add_roles = add_without_updating_cache
        removed = []

        async def remove_without_updating_cache(*roles, **kwargs):
            removed.extend(roles)

        member.remove_roles = remove_without_updating_cache
        await self.add(rr, RED, member)
        self.assertEqual(member.roles, [self.guild.default_role])
        await self.add(rr, BLUE, member)
        self.assertEqual(added, [self.red, self.blue])
        self.assertEqual(removed, [self.red])
        member.add_roles = real_add

    async def test_a_moderators_change_counts_once_the_gateway_has_caught_up(self):
        rr = await self.make((RED, self.red), mode="buttons")
        member = self.newcomer()
        await self.click(rr, member, 0)
        self.assertIn(self.red, member.roles)
        await self.click(rr, member, 0)
        self.assertNotIn(self.red, member.roles)
        await self.click(rr, member, 0)
        await self.rr.on_member_update(member, member)  # the gateway reports the bot's own change
        await member.remove_roles(self.red)  # then a moderator takes the role
        text = await self.click(rr, member, 0)
        self.assertIn("You now have", text)
        self.assertIn(self.red, member.roles)

    async def test_a_slow_member_doesnt_hold_up_another(self):
        rr = await self.make((RED, self.red))
        slow = self.newcomer()
        quick = self.newcomer()
        gate = asyncio.Event()
        real_add = slow.add_roles

        async def wait_then_add(*roles, **kwargs):
            await gate.wait()
            await real_add(*roles, **kwargs)

        slow.add_roles = wait_then_add
        await self.rr.on_raw_reaction_add(self.reacted(rr, RED, slow))
        await self.rr.on_raw_reaction_add(self.reacted(rr, RED, quick))
        for _ in range(50):
            await asyncio.sleep(0.001)
            if self.red in quick.roles:
                break
        self.assertIn(self.red, quick.roles)
        self.assertNotIn(self.red, slow.roles)
        gate.set()
        await self.rr.settle()
        self.assertIn(self.red, slow.roles)

    async def test_a_discord_failure_leaves_the_reaction_and_tries_again(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        real_add = member.add_roles
        fails = [discord.HTTPException(_FakeResponse(503), "down")]

        async def flaky(*roles, **kwargs):
            if fails:
                raise fails.pop()
            await real_add(*roles, **kwargs)

        member.add_roles = flaky
        await self.add(rr, CHECK, member)
        self.assertNotIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [member.id])
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 61), 1)
        await self.rr.settle()
        self.assertIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_a_refused_role_doesnt_retry(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()

        async def forbidden(*roles, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")

        member.add_roles = forbidden
        await self.add(rr, CHECK, member)
        self.assertEqual(self.reactors(rr, CHECK), [member.id])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_retry'"), 0)

    async def test_a_member_who_left_is_handled_without_error(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        event = self.reacted(rr, CHECK, member)
        event.member = None
        self.guild.members.remove(member)
        await self.rr.on_raw_reaction_add(event)
        await self.rr.settle()
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_queues_dont_pile_up(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        with mock.patch.object(module, "QUEUE_MEMORY", 0.01):
            await self.add(rr, RED, member)
            await asyncio.sleep(0.05)
        self.assertEqual(self.rr._queues, {})


class CatchUpTest(Base):
    """The bot was down while these reactions came and went."""

    async def restart(self) -> ReactionRoles:
        """A second copy of the module on the same database. Whatever it knows, it read from there."""
        fresh = ReactionRoles(self.bot)
        await fresh.reconcile_all()
        await fresh.settle()
        return fresh

    async def test_a_verify_reaction_during_downtime_gets_its_role(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        self.reacted(rr, CHECK, member)
        await self.restart()
        self.assertIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_verify_leaves_another_bots_reaction_alone(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        other_bot = FakeMember(self.guild, "old bot", bot=True)
        self.reacted(rr, CHECK, other_bot)
        await self.restart()
        self.assertNotIn(self.verified, other_bot.roles)
        self.assertEqual(self.reactors(rr, CHECK), [other_bot.id])

    async def test_verify_clears_a_reaction_from_someone_who_left(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        gone = self.newcomer()
        self.reacted(rr, CHECK, gone)
        self.guild.members.remove(gone)
        await self.restart()
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_a_new_reaction_gets_its_role_and_a_removed_one_loses_it(self):
        rr = await self.make((RED, self.red))
        stayed, left = self.newcomer(), self.newcomer()
        await self.add(rr, RED, stayed)
        await self.add(rr, RED, left)
        joined = self.newcomer()
        self.reacted(rr, RED, joined)
        self.unreacted(rr, RED, left)
        calls_before = len(stayed.calls)
        await self.restart()
        self.assertIn(self.red, joined.roles)
        self.assertNotIn(self.red, left.roles)
        self.assertIn(self.red, stayed.roles)
        self.assertEqual(len(stayed.calls), calls_before)

    async def test_a_moderator_taking_a_role_stays_taken_across_restarts(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        await member.remove_roles(self.red)
        other = self.newcomer()
        self.reacted(rr, RED, other)  # makes the counts differ, so every reaction on this emoji gets looked at
        await self.restart()
        self.assertNotIn(self.red, member.roles)
        self.assertIn(self.red, other.roles)

    async def test_nothing_is_read_when_the_counts_match(self):
        rr = await self.make((RED, self.red))
        await self.add(rr, RED, self.newcomer())
        fresh = ReactionRoles(self.bot)
        with mock.patch.object(self.ops, "reaction_users", side_effect=AssertionError("walked")):
            await fresh.reconcile_all(thorough=False)

    async def test_an_existing_message_gets_a_baseline_and_nobodys_roles_change(self):
        sent = await self.ops.send_message(self.channel.id, {"content": "old post", "embeds": [], "buttons": []})
        early = self.newcomer()
        self.ops.react_as(sent["id"], RED, early.id)
        client = await self.client()
        response = await client.post(self.path("/reactionroles"), json={"create_mode": "id", "channel_id": str(self.channel.id), "message_id": str(sent["id"]), "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]})
        self.assertEqual(response.status, 200)
        await self.rr.settle()
        self.assertNotIn(self.red, early.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (sent["id"],)), 1)
        await self.restart()
        self.assertNotIn(self.red, early.roles)

    async def test_a_clear_during_downtime_costs_nobody_a_role(self):
        rr = await self.make((RED, self.red))
        members = [self.newcomer() for _ in range(module.MASS_CLEAR + 5)]
        for member in members:
            await self.add(rr, RED, member)
        await self.ops.clear_reactions(rr.channel_id, rr.message_id)
        await self.restart()
        for member in members:
            self.assertIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (rr.message_id,)), 0)

    async def test_a_short_list_from_discord_doesnt_read_as_people_leaving(self):
        rr = await self.make((RED, self.red))
        members = [self.newcomer() for _ in range(40)]
        for member in members:
            await self.add(rr, RED, member)
        self.ops.react_as(rr.message_id, RED, self.newcomer().id)
        real = self.ops.reaction_users

        async def cut_short(*args, **kwargs):
            rows = await real(*args, **kwargs)
            return rows[:5]

        with mock.patch.object(self.ops, "reaction_users", side_effect=cut_short):
            await self.restart()
        for member in members:
            self.assertIn(self.red, member.roles)

    async def test_a_failed_read_changes_nothing(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.ops.react_as(rr.message_id, RED, self.newcomer().id)
        with mock.patch.object(self.ops, "reaction_users", side_effect=OpsError("Discord refused")):
            await self.restart()
        self.assertIn(self.red, member.roles)

    async def test_a_message_deleted_during_downtime_goes(self):
        rr = await self.make((RED, self.red))
        del self.ops.messages[rr.message_id]
        await self.restart()
        self.assertIsNone(await self.rr.get(rr.message_id))

    async def test_a_channel_it_cant_read_keeps_the_setup(self):
        rr = await self.make((RED, self.red))
        with mock.patch.object(self.ops, "fetch_message", side_effect=OpsError("The bot can't read that channel's history.")):
            await self.restart()
        self.assertIsNotNone(await self.rr.get(rr.message_id))

    async def test_missing_reactions_are_put_back(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue))
        await self.ops.remove_reaction(rr.channel_id, rr.message_id, RED)
        await self.restart()
        row = next(r for r in self.ops.messages[rr.message_id]["reactions"] if r["emoji"] == RED)
        self.assertTrue(row["me"])

    async def test_a_restored_backup_changes_nobodys_roles(self):
        rr = await self.make((RED, self.red))
        gone = await self.make((BLUE, self.blue))
        early = self.newcomer()
        await self.add(rr, RED, early)
        await early.remove_roles(self.red)  # a moderator took it
        await self.db.run("DELETE FROM rr_seen")
        await self.db.run("DELETE FROM rr_messages WHERE message_id = ?", (gone.message_id,))
        await self.db.run("INSERT INTO rr_seen (message_id, emoji_key, user_id) VALUES (?, 'x', 1)", (gone.message_id,))
        with mock.patch.object(module, "RESTORE_SETTLE", 0):
            await self.rr.on_guild_restored(self.guild.id)
            await self.rr.settle()
        self.assertNotIn(self.red, early.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (rr.message_id,)), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (gone.message_id,)), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_synced WHERE message_id = ?", (rr.message_id,)), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_synced WHERE message_id = ?", (gone.message_id,)), 0)
        self.assertFalse(await self.rr._needs_baseline(rr))

    async def test_a_restart_after_a_restore_still_takes_a_baseline(self):
        rr = await self.make((RED, self.red))
        early = self.newcomer()
        self.ops.react_as(rr.message_id, RED, early.id)
        await self.rr.on_guild_restored(self.guild.id)
        self.assertTrue(await self.rr._needs_baseline(rr))
        fresh = ReactionRoles(self.bot)
        await fresh.reconcile_all()
        await fresh.settle()
        self.assertNotIn(self.red, early.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (rr.message_id,)), 1)

    async def test_a_verify_message_comes_first(self):
        normal = await self.make((RED, self.red))
        verify = await self.make((CHECK, self.verified), kind="verify")
        order = []
        real = self.ops.fetch_message

        async def noting(channel_id, message_id):
            order.append(message_id)
            return await real(channel_id, message_id)

        with mock.patch.object(self.ops, "fetch_message", side_effect=noting):
            await self.restart()
        self.assertEqual(order, [verify.message_id, normal.message_id])

    async def test_on_ready_starts_it_once(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        self.reacted(rr, CHECK, member)
        await self.rr.on_ready()
        task = self.rr._reconciling
        await self.rr.on_ready()
        self.assertIs(self.rr._reconciling, task)
        await task
        await self.rr.settle()
        self.assertIn(self.verified, member.roles)

    async def test_a_later_ready_only_looks_when_counts_differ(self):
        rr = await self.make((RED, self.red))
        await self.add(rr, RED, self.newcomer())
        real = self.ops.reaction_users
        calls = []

        async def noting(*args, **kwargs):
            calls.append(args)
            return await real(*args, **kwargs)

        with mock.patch.object(self.ops, "reaction_users", side_effect=noting):
            await self.rr.on_ready()
            await self.rr._reconciling
            first = len(calls)
            self.assertGreater(first, 0)
            await self.rr.on_ready()
            await self.rr._reconciling
            self.assertEqual(len(calls), first)

    async def test_a_message_it_cant_edit_is_left_alone(self):
        rr = await self.make((RED, self.red), mode="buttons")
        self.ops.messages[rr.message_id]["controls"] = 0
        with mock.patch.object(self.ops, "edit_message", side_effect=OpsError("Discord refused the edit")):
            await self.restart()
        self.assertIsNotNone(await self.rr.get(rr.message_id))

    async def test_buttons_put_back_if_they_went_missing(self):
        rr = await self.make((RED, self.red), mode="buttons")
        self.ops.messages[rr.message_id]["components"] = []
        self.ops.messages[rr.message_id]["controls"] = 0
        await self.restart()
        self.assertEqual(self.ops.messages[rr.message_id]["components"][0]["custom_id"], "rr:b:0")


class ButtonsAndMenusTest(Base):
    async def test_the_dynamic_items_match_their_ids(self):
        self.assertTrue(RoleButton.__discord_ui_compiled_template__.fullmatch("rr:b:12"))
        self.assertTrue(RoleMenu.__discord_ui_compiled_template__.fullmatch("rr:s:0"))
        self.assertIsNone(RoleButton.__discord_ui_compiled_template__.fullmatch("rr:b:x"))

    async def test_controls_are_posted_with_the_message(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), mode="buttons")
        stored = self.ops.messages[rr.message_id]["components"]
        self.assertEqual([c["custom_id"] for c in stored], ["rr:b:0", "rr:b:1"])
        self.assertEqual(stored[0]["label"], "Red")
        self.assertEqual(self.ops.messages[rr.message_id].get("reactions", []), [])

    async def test_a_view_can_be_built_with_link_buttons_after_the_roles(self):
        rr = await self.make((RED, self.red), mode="buttons")
        view = messages.component_view({"buttons": [{"label": "Rules", "url": "https://example.com"}]}, self.rr.render(rr))
        self.assertEqual([getattr(c, "custom_id", None) for c in view.children], ["rr:b:0", None])
        self.assertEqual(view.children[1].url, "https://example.com")

    async def test_a_menu_view_holds_every_option(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), mode="select", placeholder="Colors")
        view = messages.component_view({}, self.rr.render(rr))
        menu = view.children[0]
        self.assertEqual(menu.custom_id, "rr:s:0")
        self.assertEqual(menu.placeholder, "Colors")
        self.assertEqual([o.value for o in menu.options], ["0", "1"])
        self.assertEqual((menu.min_values, menu.max_values), (1, 2))

    async def test_a_long_menu_spills_into_a_second_one(self):
        rows = [(None, self.guild.role(f"Role {i}", 3)) for i in range(30)]
        rr = RR(new_id(), self.guild.id, self.channel.id, mode="select", options=[Option(i, "", f"row{i}", [role.id]) for i, (_, role) in enumerate(rows)])
        menus = self.rr.render(rr)
        self.assertEqual([len(m["options"]) for m in menus], [25, 5])
        self.assertEqual(menus[1]["custom_id"], "rr:s:1")
        self.assertEqual(menus[1]["options"][0]["value"], "25")

    async def test_unique_menu_takes_one_pick_and_limit_takes_its_limit(self):
        unique = await self.make((RED, self.red), (BLUE, self.blue), mode="select", kind="unique")
        self.assertEqual(self.rr.render(unique)[0]["max"], 1)
        limited = await self.make((RED, self.red), (BLUE, self.blue), (GREEN, self.green), mode="select", kind="limit", limit=2)
        self.assertEqual(self.rr.render(limited)[0]["max"], 2)

    async def test_every_type_works_as_a_button(self):
        member = self.newcomer()
        normal = await self.make((RED, self.red), mode="buttons")
        self.assertIn("You now have", await self.click(normal, member, 0))
        self.assertIn(self.red, member.roles)
        self.assertIn("You no longer have", await self.click(normal, member, 0))
        self.assertNotIn(self.red, member.roles)

        unique = await self.make((RED, self.red), (BLUE, self.blue), mode="buttons", kind="unique")
        await self.click(unique, member, 0)
        text = await self.click(unique, member, 1)
        self.assertIn("You now have", text)
        self.assertIn("You no longer have", text)
        self.assertEqual([r for r in member.roles if r in (self.red, self.blue)], [self.blue])
        await self.click(unique, member, 1)

        verify = await self.make((CHECK, self.verified), mode="buttons", kind="verify")
        await self.click(verify, member, 0)
        self.assertIn(self.verified, member.roles)
        self.assertIn("already", await self.click(verify, member, 0))
        self.assertIn(self.verified, member.roles)

        drop = await self.make((CHECK, self.verified), mode="buttons", kind="drop")
        await self.click(drop, member, 0)
        self.assertNotIn(self.verified, member.roles)
        self.assertIn("don't have", await self.click(drop, member, 0))
        self.assertNotIn(self.verified, member.roles)

        reverse = await self.make((RED, self.red), mode="buttons", kind="reversed")
        await self.click(reverse, member, 0)
        self.assertIn(self.red, member.roles)
        await self.click(reverse, member, 0)
        self.assertNotIn(self.red, member.roles)

        limited = await self.make((RED, self.red), (BLUE, self.blue), mode="buttons", kind="limit", limit=1)
        await self.click(limited, member, 0)
        self.assertIn("hold 1", await self.click(limited, member, 1))
        self.assertNotIn(self.blue, member.roles)
        await self.click(limited, member, 0)

        binding = await self.make((RED, self.red), (BLUE, self.blue), mode="buttons", kind="binding")
        await self.click(binding, member, 0)
        self.assertIn("final", await self.click(binding, member, 1))
        self.assertIn("final", await self.click(binding, member, 0))
        self.assertIn(self.red, member.roles)

    async def test_every_type_works_from_a_menu(self):
        view = object()
        member = self.newcomer()

        async def pick(rr, *slots):
            return await self.click(rr, member, *slots, view=view)

        normal = await self.make((RED, self.red), (BLUE, self.blue), mode="select")
        await pick(normal, 0, 1)
        self.assertIn(self.red, member.roles)
        self.assertIn(self.blue, member.roles)
        await pick(normal, 0, 1)
        self.assertNotIn(self.red, member.roles)
        self.assertNotIn(self.blue, member.roles)

        unique = await self.make((RED, self.red), (BLUE, self.blue), mode="select", kind="unique")
        await pick(unique, 0)
        await pick(unique, 1)
        self.assertNotIn(self.red, member.roles)
        self.assertIn(self.blue, member.roles)
        await pick(unique, 1)

        verify = await self.make((CHECK, self.verified), mode="select", kind="verify")
        await pick(verify, 0)
        self.assertIn("already", await pick(verify, 0))
        self.assertIn(self.verified, member.roles)

        drop = await self.make((CHECK, self.verified), mode="select", kind="drop")
        await pick(drop, 0)
        self.assertNotIn(self.verified, member.roles)

        reverse = await self.make((RED, self.red), mode="select", kind="reversed")
        await pick(reverse, 0)
        await pick(reverse, 0)
        self.assertNotIn(self.red, member.roles)

        limited = await self.make((RED, self.red), (BLUE, self.blue), mode="select", kind="limit", limit=1)
        await pick(limited, 0)
        self.assertIn("hold 1", await pick(limited, 1))
        await pick(limited, 0)

        binding = await self.make((RED, self.red), (BLUE, self.blue), mode="select", kind="binding")
        await pick(binding, 0)
        self.assertIn("final", await pick(binding, 1))
        await self.rr.remove(binding)
        await member.remove_roles(self.red)

        temp = await self.make((RED, self.red), mode="select", kind="temp", temp_seconds=600)
        await pick(temp, 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 601), 1)
        self.assertNotIn(self.red, member.roles)

        locked = await self.make((RED, self.red), mode="select", kind="lock")
        self.assertIn("locked", await pick(locked, 0))
        self.assertNotIn(self.red, member.roles)

    async def test_a_stale_menu_says_so(self):
        rr = await self.make((RED, self.red), mode="select")
        self.assertIn("out of date", await self.click(rr, self.newcomer(), 7))

    async def test_temp_as_a_button(self):
        member = self.newcomer()
        rr = await self.make((RED, self.red), mode="buttons", kind="temp", temp_seconds=600)
        await self.click(rr, member, 0)
        self.assertIn(self.red, member.roles)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 601), 1)
        self.assertNotIn(self.red, member.roles)

    async def test_a_menu_pick_acts_on_each_option_and_resets_the_menu(self):
        member = self.newcomer(self.red)
        rr = await self.make((RED, self.red), (BLUE, self.blue), mode="select")
        view = object()
        event = interaction(self.guild, member, rr.message_id)
        await self.rr.clicked(event, [0, 1], view=view)
        self.assertEqual(event.response.calls, [("edit", {"view": view})])
        self.assertNotIn(self.red, member.roles)
        self.assertIn(self.blue, member.roles)
        sent = event.followup.sent[0]
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(sent["allowed_mentions"].roles, False)

    async def test_lists_apply_to_buttons_and_a_click_says_why(self):
        rr = await self.make((RED, self.red), mode="buttons", whitelist=[self.verified.id])
        member = self.newcomer()
        self.assertIn("need one of these", await self.click(rr, member, 0))
        self.assertNotIn(self.red, member.roles)

    async def test_a_click_on_a_message_with_no_reaction_roles_says_so(self):
        member = self.newcomer()
        event = interaction(self.guild, member, new_id())
        await self.rr.clicked(event, [0])
        self.assertIn("anymore", event.followup.sent[0]["content"])

    async def test_a_click_too_late_for_discord_changes_nothing(self):
        rr = await self.make((RED, self.red), mode="buttons")
        member = self.newcomer()
        event = interaction(self.guild, member, rr.message_id)

        async def too_late(**kwargs):
            raise discord.NotFound(_FakeResponse(404), "Unknown interaction")

        event.response.defer = too_late
        await self.rr.clicked(event, [0])
        await self.rr.settle()
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(event.followup.sent, [])

    async def test_clicks_share_the_queue_with_reactions(self):
        linked_a = await self.make((RED, self.red))
        linked_b = await self.make((BLUE, self.blue), mode="buttons")
        await self.rr._link_group(self.guild.id, [linked_a, linked_b])
        member = self.newcomer()
        await self.add(linked_a, RED, member)
        await self.click(linked_b, member, 0)
        self.assertNotIn(self.red, member.roles)
        self.assertIn(self.blue, member.roles)
        self.assertEqual(self.reactors(linked_a, RED), [])

    async def test_an_edit_from_any_page_keeps_the_controls(self):
        rr = await self.make((RED, self.red), mode="buttons")
        self.assertEqual([c["custom_id"] for c in await self.rr.components_for(rr.message_id)], ["rr:b:0"])
        self.assertEqual(await self.rr.components_for(new_id()), [])


class MessageLifeTest(Base):
    async def test_a_deleted_message_stops_working_and_goes_after_a_week(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        await self.rr.on_raw_message_delete(SimpleNamespace(message_id=rr.message_id))
        self.assertIsNone(await self.rr.get(rr.message_id))
        self.assertFalse(await self.rr.holds_message(self.guild.id, rr.message_id))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen"), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + module.TRASH_SECONDS + 1), 1)
        self.assertIsNone(await self.rr.get(rr.message_id, trashed=True))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_pairs"), 0)

    async def test_a_bulk_delete_takes_only_the_tracked_ones(self):
        rr = await self.make((RED, self.red))
        keep = await self.make((BLUE, self.blue))
        await self.rr.on_raw_bulk_message_delete(SimpleNamespace(message_ids={rr.message_id, new_id()}))
        self.assertIsNone(await self.rr.get(rr.message_id))
        self.assertIsNotNone(await self.rr.get(keep.message_id))

    async def test_a_deleted_channel_takes_its_messages(self):
        rr = await self.make((RED, self.red))
        await self.rr.on_guild_channel_delete(SimpleNamespace(guild=self.guild, id=self.channel.id))
        self.assertIsNone(await self.rr.get(rr.message_id, trashed=True))

    async def test_a_deleted_role_leaves_the_other_rows(self):
        rr = await self.make((RED, [self.red, self.blue]), (GREEN, self.green))
        await self.rr.on_guild_role_delete(SimpleNamespace(guild=self.guild, id=self.green.id))
        found = await self.rr.get(rr.message_id)
        self.assertEqual([o.key for o in found.options], [emoji_key(RED)])
        await self.rr.on_guild_role_delete(SimpleNamespace(guild=self.guild, id=self.red.id))
        found = await self.rr.get(rr.message_id)
        self.assertEqual(found.options[0].role_ids, [self.blue.id])
        await self.rr.on_guild_role_delete(SimpleNamespace(guild=self.guild, id=self.blue.id))
        self.assertIsNone(await self.rr.get(rr.message_id))

    async def test_self_destruct(self):
        rr = await self.make((RED, self.red), destruct_at=time.time() + 60)
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 61), 1)
        self.assertNotIn(rr.message_id, self.ops.messages)
        self.assertIsNone(await self.rr.get(rr.message_id, trashed=True))

    async def test_reactions_are_put_back_after_someone_clears_them(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue))
        member = self.newcomer()
        await self.add(rr, RED, member)
        await self.ops.clear_reactions(rr.channel_id, rr.message_id)
        await self.rr.on_raw_reaction_clear(SimpleNamespace(message_id=rr.message_id, channel_id=rr.channel_id, guild_id=self.guild.id))
        emojis = {r["emoji"] for r in self.ops.messages[rr.message_id]["reactions"] if r["me"]}
        self.assertEqual(emojis, {RED, BLUE})
        self.assertIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen"), 0)

    async def test_one_cleared_emoji_is_put_back(self):
        rr = await self.make((RED, self.red))
        await self.ops.clear_reaction(rr.channel_id, rr.message_id, RED)
        await self.rr.on_raw_reaction_clear_emoji(SimpleNamespace(message_id=rr.message_id, channel_id=rr.channel_id, guild_id=self.guild.id, emoji=discord.PartialEmoji(name=RED)))
        self.assertTrue(self.ops.messages[rr.message_id]["reactions"][0]["me"])


class MoveTest(Base):
    async def test_move_carries_everything_to_the_repost(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue), kind="unique", whitelist=[self.verified.id], destruct_at=time.time() + 3600)
        member = self.newcomer(self.verified)
        await self.add(rr, RED, member)
        repost = await self.ops.send_message(self.channel.id, {"content": "new", "embeds": [], "buttons": []})
        moved = await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, repost["id"])
        self.assertEqual(moved["message_id"], repost["id"])
        self.assertIsNone(await self.rr.get(rr.message_id))
        found = await self.rr.get(repost["id"])
        self.assertEqual(found.kind, "unique")
        self.assertEqual(found.whitelist, [self.verified.id])
        self.assertEqual({r["emoji"] for r in self.ops.messages[repost["id"]]["reactions"] if r["me"]}, {RED, BLUE})
        self.assertIsNotNone(await self.bot.timers.get_key("rr_destruct", str(repost["id"])))
        self.assertIsNone(await self.bot.timers.get_key("rr_destruct", str(rr.message_id)))
        self.assertEqual(self.ops.messages[rr.message_id].get("reactions", []), [])
        await self.add(found, BLUE, member)
        self.assertIn(self.blue, member.roles)
        self.assertNotIn(self.red, member.roles)

    async def test_a_message_with_none_returns_nothing_and_changes_nothing(self):
        repost = await self.ops.send_message(self.channel.id, {"content": "new", "embeds": [], "buttons": []})
        self.assertIsNone(await self.rr.move_message(self.guild.id, new_id(), self.channel.id, repost["id"]))
        self.assertIsNone(await self.rr.get(repost["id"]))

    async def test_another_servers_message_returns_nothing(self):
        rr = await self.make((RED, self.red))
        repost = await self.ops.send_message(self.channel.id, {"content": "new", "embeds": [], "buttons": []})
        self.assertIsNone(await self.rr.move_message(new_id(), rr.message_id, self.channel.id, repost["id"]))
        self.assertIsNotNone(await self.rr.get(rr.message_id))

    async def test_buttons_are_redrawn_on_the_new_message_and_taken_off_the_old(self):
        rr = await self.make((RED, self.red), mode="buttons")
        repost = await self.ops.send_message(self.channel.id, {"content": "new", "embeds": [], "buttons": []})
        await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, repost["id"])
        self.assertEqual(self.ops.messages[repost["id"]]["components"][0]["custom_id"], "rr:b:0")
        self.assertEqual(self.ops.messages[rr.message_id]["components"], [])

    async def test_buttons_cant_move_onto_another_bots_message(self):
        rr = await self.make((RED, self.red), mode="buttons")
        theirs = await self.ops.send_message(self.channel.id, {"content": "theirs", "embeds": [], "buttons": []})
        self.ops.messages[theirs["id"]]["mine"] = False
        with self.assertRaises(Invalid):
            await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, theirs["id"])
        self.assertIsNotNone(await self.rr.get(rr.message_id))

    async def test_a_target_that_has_some_already_is_refused(self):
        first = await self.make((RED, self.red))
        second = await self.make((BLUE, self.blue))
        with self.assertRaises(Invalid):
            await self.rr.move_message(self.guild.id, first.message_id, self.channel.id, second.message_id)

    async def test_move_rescues_a_message_deleted_this_week(self):
        rr = await self.make((RED, self.red))
        await self.rr.on_raw_message_delete(SimpleNamespace(message_id=rr.message_id))
        del self.ops.messages[rr.message_id]
        repost = await self.ops.send_message(self.channel.id, {"content": "new", "embeds": [], "buttons": []})
        await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, repost["id"])
        self.assertIsNotNone(await self.rr.get(repost["id"]))
        self.assertIsNone(await self.bot.timers.get_key("rr_purge", str(rr.message_id)))
        self.assertEqual(await self.bot.timers.get_key("rr_purge", str(repost["id"])), None)

    async def test_a_rescued_message_doesnt_inherit_a_self_destruct_that_ran_out(self):
        rr = await self.make((RED, self.red), destruct_at=time.time() + 60)
        await self.rr.on_raw_message_delete(SimpleNamespace(message_id=rr.message_id))
        await self.db.run("UPDATE rr_messages SET destruct_at = ? WHERE message_id = ?", (time.time() - 5, rr.message_id))
        repost = await self.ops.send_message(self.channel.id, {"content": "new", "embeds": [], "buttons": []})
        await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, repost["id"])
        self.assertIsNone((await self.rr.get(repost["id"])).destruct_at)
        self.assertIsNone(await self.bot.timers.get_key("rr_destruct", str(repost["id"])))

    async def test_a_repost_keeps_existing_reactions_as_a_baseline(self):
        rr = await self.make((RED, self.red))
        theirs = await self.ops.send_message(self.channel.id, {"content": "theirs", "embeds": [], "buttons": []})
        self.ops.messages[theirs["id"]]["mine"] = False
        early = self.newcomer()
        self.ops.react_as(theirs["id"], RED, early.id)
        await self.rr.move_message(self.guild.id, rr.message_id, self.channel.id, theirs["id"])
        await self.rr.settle()
        self.assertNotIn(self.red, early.roles)
        self.assertTrue((await self.rr.get(theirs["id"])).foreign)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (theirs["id"],)), 1)


class ParsingTest(Base):
    def test_emoji_forms(self):
        self.assertEqual(clean_emoji(CHECK), CHECK)
        self.assertEqual(clean_emoji("<:party:123456789012345678>"), "<:party:123456789012345678>")
        self.assertEqual(clean_emoji("party:123456789012345678"), "<:party:123456789012345678>")
        self.assertEqual(clean_emoji("a:party:123456789012345678"), "<a:party:123456789012345678>")
        self.assertEqual(clean_emoji("1\ufe0f\u20e3"), "1\ufe0f\u20e3")
        for bad in ("", "hello", "abc def", None, 5, "1"):
            with self.assertRaises(Invalid):
                clean_emoji(bad)

    def test_keys(self):
        self.assertEqual(emoji_key("<a:party:123456789012345678>"), "123456789012345678")
        self.assertEqual(emoji_key("\N{HEAVY BLACK HEART}\ufe0f"), "\N{HEAVY BLACK HEART}")
        self.assertEqual(key_of(discord.PartialEmoji(name="x", id=5)), "5")
        self.assertEqual(key_of(discord.PartialEmoji(name="\N{HEAVY BLACK HEART}\ufe0f")), "\N{HEAVY BLACK HEART}")

    def test_message_refs(self):
        self.assertEqual(parse_message_ref("123456789012345678"), (None, None, 123456789012345678))
        self.assertEqual(parse_message_ref("https://discord.com/channels/111111111111111111/222222222222222222/333333333333333333"), (111111111111111111, 222222222222222222, 333333333333333333))
        self.assertEqual(parse_message_ref("https://canary.discord.com/channels/111111111111111111/222222222222222222/333333333333333333")[2], 333333333333333333)
        self.assertIsNone(parse_message_ref("test"))
        self.assertIsNone(parse_message_ref("https://evil.example/channels/1/2/3"))

    async def test_pairs_read_mentions_and_names_with_spaces(self):
        two_words = self.guild.role("Two Words", 2)
        emoji_named = self.guild.role("\N{VIDEO GAME} Gamer", 2)
        pairs, problem = parse_pairs(self.guild, f"{RED} <@&{self.red.id}>\n{BLUE} two words {GREEN} {self.green.id}")
        self.assertIsNone(problem)
        self.assertEqual([(e, r) for e, r in pairs], [(RED, self.red), (BLUE, two_words), (GREEN, self.green)])
        pairs, problem = parse_pairs(self.guild, f"{RED} \N{VIDEO GAME} Gamer {BLUE} Blue")
        self.assertIsNone(problem)
        self.assertEqual(pairs, [(RED, emoji_named), (BLUE, self.blue)])
        pairs, problem = parse_pairs(self.guild, f"{RED}<@&{self.red.id}>")
        self.assertIsNone(problem)
        pairs, problem = parse_pairs(self.guild, f"<:party:123456789012345678>Red")
        self.assertIsNone(problem)
        self.assertEqual(pairs[0][1], self.red)

    async def test_pairs_explain_what_went_wrong(self):
        self.assertIn("isn't an emoji", parse_pairs(self.guild, "hello Red")[1])
        self.assertIn("no role called", parse_pairs(self.guild, f"{RED} Nothing Like It")[1])
        self.assertIn("needs a role", parse_pairs(self.guild, RED)[1])

    async def test_all_in_one_text(self):
        wording, pairs = split_text_and_pairs(self.guild, f"title | description {RED} Red")
        self.assertEqual(wording, "title | description")
        self.assertEqual(pairs, [(RED, self.red)])
        wording, pairs = split_text_and_pairs(self.guild, f'"title | {BLUE} and more" {RED} Red')
        self.assertEqual(wording, f"title | {BLUE} and more")
        wording, pairs = split_text_and_pairs(self.guild, f"\N{VIDEO GAME} Games | pick {RED} Red\n{BLUE} Blue")
        self.assertEqual(wording, "\N{VIDEO GAME} Games | pick")
        self.assertEqual(len(pairs), 2)
        with self.assertRaises(Invalid):
            split_text_and_pairs(self.guild, "no pairs at all")


class CommandsTest(Base):
    async def post(self) -> int:
        return (await self.ops.send_message(self.channel.id, {"content": "post", "embeds": [{"title": "Old", "description": "Words"}], "buttons": []}))["id"]

    async def test_add_makes_a_message_a_reaction_role_and_reacts(self):
        message_id = await self.post()
        ctx = self.context()
        await self.rr.rr_add.callback(self.rr, ctx, str(message_id), RED, role=self.red.mention)
        self.assertEqual(ctx.fails, [])
        found = await self.rr.get(message_id)
        self.assertEqual(found.options[0].role_ids, [self.red.id])
        self.assertTrue(self.ops.messages[message_id]["reactions"][0]["me"])
        await self.rr.settle()
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "reactionrole.add")

    async def test_add_takes_the_channel_first_and_last_as_text_forms_do(self):
        other = FakeChannel(self.guild, "roles")
        self.ops.channels.append(self._row(other))
        sent = await self.ops.send_message(other.id, {"content": "x", "embeds": [], "buttons": []})
        ctx = self.context()
        await self.rr.rr_add.callback(self.rr, ctx, other.mention, str(sent["id"]), role=f"{RED} Red")
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.rr.get(sent["id"])).channel_id, other.id)
        sent = await self.ops.send_message(other.id, {"content": "y", "embeds": [], "buttons": []})
        ctx = self.context()
        await self.rr.rr_add.callback(self.rr, ctx, str(sent["id"]), RED, role=f"Red {other.mention}")
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.rr.get(sent["id"])).channel_id, other.id)

    async def test_add_with_no_message_uses_the_latest_in_the_channel(self):
        await self.post()
        latest = await self.post()
        ctx = self.context()
        ctx.message.id = latest + 1  # the command is newer than the post, as it is on Discord
        await self.rr.rr_add.callback(self.rr, ctx, RED, "Red", role="")
        self.assertEqual(ctx.fails, [])
        self.assertIsNotNone(await self.rr.get(latest))

    async def test_add_on_another_bots_message_says_its_text_cant_change(self):
        message_id = await self.post()
        self.ops.messages[message_id]["mine"] = False
        ctx = self.context()
        await self.rr.rr_add.callback(self.rr, ctx, str(message_id), RED, role="Red")
        self.assertTrue((await self.rr.get(message_id)).foreign)
        self.assertIn("Another bot wrote", ctx.oks[0])

    async def test_add_refuses_roles_nobody_here_may_hand_out(self):
        message_id = await self.post()
        high = self.guild.role("High", 20)
        managed = self.guild.role("Managed", 2, managed=True)
        for role in (high, managed):
            ctx = self.context()
            await self.rr.rr_add.callback(self.rr, ctx, str(message_id), RED, role=role.mention)
            self.assertEqual(len(ctx.fails), 1)
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)("view_channel")
        ctx = self.context(author=self.mod)
        above_mod = self.guild.role("Above Mod", 6)
        await self.rr.rr_add.callback(self.rr, ctx, str(message_id), RED, role=above_mod.mention)
        self.assertIn("your highest role", ctx.fails[0])
        self.assertIsNone(await self.rr.get(message_id))

    async def test_add_warns_about_a_role_with_dangerous_permissions(self):
        message_id = await self.post()
        strong = self.guild.role("Strong", 3, "ban_members")
        ctx = self.context()
        await self.rr.rr_add.callback(self.rr, ctx, str(message_id), RED, role=strong.mention)
        self.assertIn("elevated", ctx.oks[0])

    async def test_a_message_in_another_server_is_refused(self):
        rr = await self.make((RED, self.red))
        await self.db.run("UPDATE rr_messages SET guild_id = ? WHERE message_id = ?", (new_id(), rr.message_id))
        ctx = self.context()
        await self.rr.rr_add.callback(self.rr, ctx, str(rr.message_id), BLUE, role="Blue")
        self.assertIn("another server", ctx.fails[0])
        ctx = self.context()
        await self.rr.rr_normal.callback(self.rr, ctx, str(rr.message_id))
        self.assertEqual(len(ctx.fails), 1)
        ctx = self.context()
        link = f"https://discord.com/channels/{new_id()}/{self.channel.id}/{await self.post()}"
        await self.rr.rr_add.callback(self.rr, ctx, link, BLUE, role="Blue")
        self.assertIn("another server", ctx.fails[0])

    async def test_more_than_twenty_reactions_is_refused(self):
        message_id = await self.post()
        ctx = self.context()
        rows = " ".join(f"{chr(0x1F600 + i)} Red" for i in range(21))
        await self.rr.rr_addmany.callback(self.rr, ctx, str(message_id), pairs=rows)
        self.assertIn("20 different", ctx.fails[0])

    async def test_addmany(self):
        message_id = await self.post()
        ctx = self.context()
        await self.rr.rr_addmany.callback(self.rr, ctx, str(message_id), pairs=f"{RED} Red\n{BLUE} Blue")
        self.assertEqual(ctx.fails, [])
        self.assertEqual(len((await self.rr.get(message_id)).options), 2)

    async def test_remove_from_one_message_or_all(self):
        first = await self.make((RED, self.red), (BLUE, self.blue))
        second = await self.make((RED, self.red))
        ctx = self.context()
        await self.rr.rr_remove.callback(self.rr, ctx, role="Red", message_id=str(first.message_id))
        self.assertEqual([o.key for o in (await self.rr.get(first.message_id)).options], [emoji_key(BLUE)])
        self.assertIsNotNone(await self.rr.get(second.message_id))
        ctx = self.context()
        await self.rr.rr_remove.callback(self.rr, ctx, role="Red")
        self.assertIsNone(await self.rr.get(second.message_id))

    async def test_remove_reads_the_text_form_with_the_message_first(self):
        rr = await self.make((RED, self.red))
        ctx = self.context()
        await self.rr.rr_remove.callback(self.rr, ctx, role=f"{rr.message_id} Red")
        self.assertEqual(ctx.fails, [])
        self.assertIsNone(await self.rr.get(rr.message_id))

    async def test_purge_one(self):
        rr = await self.make((RED, self.red))
        ctx = self.context()
        await self.rr.rr_purge.callback(self.rr, ctx, str(rr.message_id))
        self.assertIsNone(await self.rr.get(rr.message_id))
        self.assertIn(rr.message_id, self.ops.messages)
        self.assertEqual(self.ops.messages[rr.message_id].get("reactions"), [])

    async def test_purge_all_asks_first(self):
        rr = await self.make((RED, self.red))
        ctx = self.context()
        with mock.patch.object(self.rr, "_confirm", return_value=False):
            await self.rr.rr_purge.callback(self.rr, ctx, None)
        self.assertIsNotNone(await self.rr.get(rr.message_id))
        with mock.patch.object(self.rr, "_confirm", return_value=True):
            await self.rr.rr_purge.callback(self.rr, self.context(), None)
        self.assertIsNone(await self.rr.get(rr.message_id))

    async def test_blacklist_and_whitelist_choices(self):
        rr = await self.make((RED, self.red))
        ctx = self.context()
        await self.rr.rr_blacklist.callback(self.rr, ctx, "add", str(rr.message_id), roles="Muted, Member")
        self.assertEqual((await self.rr.get(rr.message_id)).blacklist, [self.role_muted.id, self.role_member.id])
        await self.rr.rr_blacklist.callback(self.rr, ctx, "remove", str(rr.message_id), roles="Muted")
        self.assertEqual((await self.rr.get(rr.message_id)).blacklist, [self.role_member.id])
        await self.rr.rr_blacklist.callback(self.rr, ctx, "clear", str(rr.message_id))
        self.assertEqual((await self.rr.get(rr.message_id)).blacklist, [])
        await self.rr.rr_whitelist.callback(self.rr, ctx, "add", str(rr.message_id), roles="Nothing Like It")
        self.assertIn("Couldn't find", ctx.fails[0])
        await self.rr.rr_wl.callback(self.rr, ctx, str(rr.message_id), roles="Verified")
        self.assertEqual((await self.rr.get(rr.message_id)).whitelist, [self.verified.id])
        await self.rr.rr_unwl.callback(self.rr, ctx, str(rr.message_id), roles="Verified")
        await self.rr.rr_bl.callback(self.rr, ctx, str(rr.message_id), roles="Muted")
        await self.rr.rr_clearbl.callback(self.rr, ctx, str(rr.message_id))
        found = await self.rr.get(rr.message_id)
        self.assertEqual((found.whitelist, found.blacklist), ([], []))

    async def test_types_by_command(self):
        rr = await self.make((RED, self.red))
        ctx = self.context()
        for name in ("rr_unique", "rr_binding", "rr_verify", "rr_drop", "rr_reversed", "rr_lock"):
            await getattr(self.rr, name).callback(self.rr, ctx, str(rr.message_id))
            self.assertEqual((await self.rr.get(rr.message_id)).kind, name[3:])
        await self.rr.rr_normal.callback(self.rr, ctx, str(rr.message_id))
        self.assertEqual((await self.rr.get(rr.message_id)).kind, "normal")
        await self.rr.rr_limit.callback(self.rr, ctx, str(rr.message_id), 3)
        found = await self.rr.get(rr.message_id)
        self.assertEqual((found.kind, found.limit), ("limit", 3))
        await self.rr.rr_temp.callback(self.rr, ctx, str(rr.message_id), duration="2h")
        found = await self.rr.get(rr.message_id)
        self.assertEqual((found.kind, found.temp_seconds), ("temp", 7200))
        await self.rr.rr_modify.callback(self.rr, ctx, "unique", str(rr.message_id))
        self.assertEqual((await self.rr.get(rr.message_id)).kind, "unique")
        await self.rr.rr_modify.callback(self.rr, ctx, "temp", str(rr.message_id), value="nonsense")
        self.assertIn("temp type", ctx.fails[0])

    async def test_a_message_that_turns_verify_loses_its_old_reactions(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        ctx = self.context()
        await self.rr.rr_verify.callback(self.rr, ctx, str(rr.message_id))
        self.assertEqual(self.reactors(rr, RED), [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen"), 0)
        self.assertTrue(self.ops.messages[rr.message_id]["reactions"][0]["me"])

    async def test_self_destruct_and_cancel(self):
        rr = await self.make((RED, self.red))
        ctx = self.context()
        await self.rr.rr_selfdestruct.callback(self.rr, ctx, str(rr.message_id), when="1h")
        self.assertIsNotNone(await self.bot.timers.get_key("rr_destruct", str(rr.message_id)))
        await self.rr.rr_selfdestruct.callback(self.rr, ctx, str(rr.message_id), when="off")
        self.assertIsNone(await self.bot.timers.get_key("rr_destruct", str(rr.message_id)))
        await self.rr.rr_selfdestruct.callback(self.rr, ctx, str(rr.message_id), when="soon")
        self.assertEqual(len(ctx.fails), 1)

    async def test_edit_changes_the_title_and_description(self):
        message_id = await self.post()
        rr = RR(message_id, self.guild.id, self.channel.id, created_at=time.time(), options=[Option(0, RED, emoji_key(RED), [self.red.id])])
        await self.rr._save(rr, None, fresh=True)
        ctx = self.context()
        await self.rr.rr_edit.callback(self.rr, ctx, str(message_id), message_string="New | Text")
        embed = self.ops.messages[message_id]["payload"]["embeds"][0]
        self.assertEqual((embed["title"], embed["description"]), ("New", "Text"))
        await self.rr.rr_edit.callback(self.rr, ctx, str(message_id), message_string="Only title")
        embed = self.ops.messages[message_id]["payload"]["embeds"][0]
        self.assertEqual((embed["title"], embed["description"]), ("Only title", "Text"))

    async def test_edit_and_color_refuse_another_bots_message(self):
        rr = await self.make((RED, self.red), foreign=True)
        ctx = self.context()
        await self.rr.rr_edit.callback(self.rr, ctx, str(rr.message_id), message_string="New")
        self.assertIn("Another bot", ctx.fails[0])
        await self.rr.rr_color.callback(self.rr, ctx, str(rr.message_id), "ff0000")
        self.assertIn("Another bot", ctx.fails[1])

    async def test_color(self):
        message_id = await self.post()
        ctx = self.context()
        await self.rr.rr_color.callback(self.rr, ctx, str(message_id), "#FF0000")
        self.assertEqual(self.ops.messages[message_id]["payload"]["embeds"][0]["color"], 0xFF0000)
        plain = await self.make((RED, self.red))
        await self.rr.rr_color.callback(self.rr, ctx, str(plain.message_id), "#FF0000")
        self.assertIn("no embed", ctx.fails[0])

    async def test_channel_is_made_with_the_right_permissions(self):
        ctx = self.context()
        await self.rr.rr_channel.callback(self.rr, ctx, channel_name="color-roles")
        channel = next(c for c in self.guild.channels if c.name == "color-roles")
        everyone = channel.overwrites[self.guild.default_role]
        self.assertFalse(everyone.send_messages)
        self.assertFalse(everyone.add_reactions)
        self.assertTrue(channel.overwrites[self.me].add_reactions)

    async def test_fix_puts_reactions_back(self):
        rr = await self.make((RED, self.red))
        await self.ops.remove_reaction(rr.channel_id, rr.message_id, RED)
        ctx = self.context()
        await self.rr.rr_fix.callback(self.rr, ctx, None)
        self.assertTrue(self.ops.messages[rr.message_id]["reactions"][0]["me"])

    async def test_fixforeign_joins_reactions_someone_added(self):
        message_id = await self.post()
        self.ops.react_as(message_id, "<:other:123456789012345678>", self.member.id)
        ctx = self.context()
        await self.rr.rr_fixforeign.callback(self.rr, ctx, str(message_id))
        self.assertTrue(self.ops.messages[message_id]["reactions"][0]["me"])
        ctx = self.context()
        await self.rr.rr_fixforeign.callback(self.rr, ctx, str(message_id))
        self.assertEqual(len(ctx.fails), 1)

    async def test_link_unlink_links(self):
        first = await self.make((RED, self.red))
        second = await self.make((BLUE, self.blue))
        third = await self.make((GREEN, self.green))
        ctx = self.context()
        await self.rr.rr_link.callback(self.rr, ctx, message_ids=f"{first.message_id} {second.message_id}")
        await self.rr.rr_link.callback(self.rr, ctx, message_ids=f"{second.message_id}, {third.message_id}")
        groups = {(await self.rr.get(x.message_id)).group_id for x in (first, second, third)}
        self.assertEqual(len(groups), 1)
        await self.rr.rr_links.callback(self.rr, ctx)
        self.assertIn(str(first.message_id), ctx.sent[-1]["embed"].description)
        await self.rr.rr_unlink.callback(self.rr, ctx, str(first.message_id))
        self.assertIsNone((await self.rr.get(third.message_id)).group_id)
        await self.rr.rr_link.callback(self.rr, ctx, message_ids=str(first.message_id))
        self.assertIn("at least two", ctx.fails[-1])

    async def test_a_group_of_one_dissolves_when_a_message_goes(self):
        first = await self.make((RED, self.red))
        second = await self.make((BLUE, self.blue))
        await self.rr._link_group(self.guild.id, [first, second])
        await self.rr.remove(first)
        self.assertIsNone((await self.rr.get(second.message_id)).group_id)

    async def test_move_command(self):
        rr = await self.make((RED, self.red))
        target = await self.post()
        ctx = self.context()
        await self.rr.rr_move.callback(self.rr, ctx, str(rr.message_id), str(target))
        self.assertEqual(ctx.fails, [])
        self.assertIsNotNone(await self.rr.get(target))

    async def test_all_in_one(self):
        ctx = self.context()
        await self.rr.rr_aio.callback(self.rr, ctx, "default", "FF0000", text=f"Title | Words {RED} Red {BLUE} Blue")
        self.assertEqual(ctx.fails, [])
        message_id = max(self.ops.messages)
        embed = self.ops.messages[message_id]["payload"]["embeds"][0]
        self.assertEqual((embed["title"], embed["description"], embed["color"]), ("Title", "Words", 0xFF0000))
        self.assertEqual(len((await self.rr.get(message_id)).options), 2)

    async def test_all_in_one_text_forms(self):
        other = FakeChannel(self.guild, "roles")
        self.ops.channels.append(self._row(other))
        ctx = self.context()
        await self.rr.rr_aio.callback(self.rr, ctx, other.mention, "none", text=f'"Title | Words" {RED} Red')
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.rr.get(max(self.ops.messages))).channel_id, other.id)
        ctx = self.context()
        await self.rr.rr_aiov.callback(self.rr, ctx, other, "none", text=f"Rules | Agree {CHECK} Verified")
        self.assertEqual((await self.rr.get(max(self.ops.messages))).kind, "verify")
        await self.rr.rr_aiou.callback(self.rr, ctx, other, "none", text=f"Colors | Pick {RED} Red")
        self.assertEqual((await self.rr.get(max(self.ops.messages))).kind, "unique")
        await self.rr.rr_aioi.callback(self.rr, ctx, other, "none", text=f"Drop | Bye {RED} Red")
        self.assertEqual((await self.rr.get(max(self.ops.messages))).kind, "drop")
        ctx = self.context()
        await self.rr.rr_aio.callback(self.rr, ctx, "nonsense", "none", text=f"T {RED} Red")
        self.assertEqual(len(ctx.fails), 1)

    async def test_maxroles(self):
        rr = await self.make((RED, self.red), (BLUE, self.blue))
        ctx = self.context()
        await self.rr.rr_maxroles.callback(self.rr, ctx, str(rr.message_id), roles_and_limits="Red 10 Blue 5")
        found = await self.rr.get(rr.message_id)
        self.assertEqual([o.limits for o in found.options], [{self.red.id: 10}, {self.blue.id: 5}])
        await self.rr.rr_maxroles.callback(self.rr, ctx, str(rr.message_id), roles_and_limits="Red 0")
        self.assertEqual((await self.rr.get(rr.message_id)).options[0].limits, {})
        await self.rr.rr_maxroles.callback(self.rr, ctx, str(rr.message_id), roles_and_limits="Green 2")
        self.assertIn("isn't on", ctx.fails[0])
        await self.rr.rr_maxroles.callback(self.rr, ctx, str(rr.message_id))
        self.assertIn("up to 5", ctx.sent[-1]["content"])

    async def test_mode_switches_reactions_for_buttons_and_back(self):
        rr = await self.make((RED, self.red))
        ctx = self.context()
        await self.rr.rr_mode.callback(self.rr, ctx, str(rr.message_id), "buttons")
        self.assertEqual(self.ops.messages[rr.message_id]["components"][0]["custom_id"], "rr:b:0")
        self.assertEqual(self.ops.messages[rr.message_id]["reactions"], [])
        await self.rr.rr_mode.callback(self.rr, ctx, str(rr.message_id), "select")
        self.assertEqual(self.ops.messages[rr.message_id]["components"][0]["custom_id"], "rr:s:0")
        await self.rr.rr_mode.callback(self.rr, ctx, str(rr.message_id), "reactions")
        self.assertEqual(self.ops.messages[rr.message_id]["components"], [])
        self.assertTrue(self.ops.messages[rr.message_id]["reactions"][0]["me"])

    async def test_mode_refuses_controls_on_another_bots_message(self):
        rr = await self.make((RED, self.red), foreign=True)
        ctx = self.context()
        await self.rr.rr_mode.callback(self.rr, ctx, str(rr.message_id), "buttons")
        self.assertIn("Another bot", ctx.fails[0])
        self.assertEqual((await self.rr.get(rr.message_id)).mode, "reactions")

    async def test_show(self):
        rr = await self.make((RED, self.red), whitelist=[self.verified.id])
        self.guild.default_role.permissions = type(self.guild.default_role.permissions)("view_channel")
        ctx = self.context(author=self.member)
        await self.rr.rr_show.callback(self.rr, ctx)
        self.assertIn(str(rr.message_id), ctx.sent[0]["embed"].description)
        self.assertFalse(ctx.sent[0]["allowed_mentions"].roles)
        ctx = self.context(author=self.member)
        await self.rr.rr_show.callback(self.rr, ctx, str(rr.message_id))
        self.assertIn("Only for", ctx.sent[0]["embed"].description)
        ctx = self.context(author=self.member)
        await self.rr.reactionrole.callback(self.rr, ctx)
        self.assertEqual(len(ctx.sent), 1)

    async def test_setup_walks_through_everything(self):
        answers = iter(["here", "Colors | Pick one", "ff0000", f"{RED} Red\n{BLUE} Blue", "done", "unique", "buttons"])
        ctx = self.context()

        async def wait_for(event, check=None, timeout=None):
            message = SimpleNamespace(content=next(answers), author=ctx.author, channel=ctx.channel)
            self.assertTrue(check(message))
            return message

        self.bot.wait_for = wait_for
        await self.rr.rr_setup.callback(self.rr, ctx)
        self.assertEqual(ctx.fails, [])
        message_id = max(self.ops.messages)
        found = await self.rr.get(message_id)
        self.assertEqual((found.kind, found.mode, len(found.options)), ("unique", "buttons", 2))
        self.assertEqual(self.ops.messages[message_id]["payload"]["embeds"][0]["color"], 0xFF0000)
        self.assertEqual(self.ops.messages[message_id]["components"][0]["custom_id"], "rr:b:0")

    async def test_setup_stops_on_cancel_and_on_a_timeout(self):
        answers = iter(["here", "cancel"])
        ctx = self.context()

        async def wait_for(event, check=None, timeout=None):
            return SimpleNamespace(content=next(answers), author=ctx.author, channel=ctx.channel)

        self.bot.wait_for = wait_for
        await self.rr.rr_setup.callback(self.rr, ctx)
        self.assertIn("canceled", ctx.oks[0])

        async def nobody_answers(event, check=None, timeout=None):
            raise asyncio.TimeoutError

        ctx = self.context()
        self.bot.wait_for = nobody_answers
        await self.rr.rr_setup.callback(self.rr, ctx)
        self.assertIn("timed out", ctx.fails[0])
        self.assertEqual(await self.rr.for_guild(self.guild.id), [])

    async def test_setup_asks_again_after_a_bad_answer(self):
        answers = iter(["#nowhere", "here", "Pick", "not a color", "none", "hello", f"{RED} Red", "done", "sideways", "limit 2", "nope", "reactions"])
        ctx = self.context()

        async def wait_for(event, check=None, timeout=None):
            return SimpleNamespace(content=next(answers), author=ctx.author, channel=ctx.channel)

        self.bot.wait_for = wait_for
        await self.rr.rr_setup.callback(self.rr, ctx)
        found = await self.rr.get(max(self.ops.messages))
        self.assertEqual((found.kind, found.limit, found.mode), ("limit", 2, "reactions"))


class DashboardTest(Base):
    def form(self, **extra) -> dict:
        return {"create_mode": "post", "channel_id": str(self.channel.id), "payload": {"content": "Pick a color", "embeds": [], "buttons": []}, "type": "normal", "mode": "reactions",
                "rows": [{"emoji": RED, "roles": [str(self.red.id)]}], **extra}

    async def test_create_posts_the_message_and_reacts(self):
        client = await self.client()
        response = await client.post(self.path("/reactionroles"), json=self.form(whitelist=[str(self.verified.id)]))
        self.assertEqual(response.status, 200)
        body = await response.json()
        message_id = int(body["message_id"])
        self.assertEqual(self.ops.messages[message_id]["payload"]["content"], "Pick a color")
        self.assertTrue(self.ops.messages[message_id]["reactions"][0]["me"])
        found = await self.rr.get(message_id)
        self.assertEqual(found.whitelist, [self.verified.id])
        self.assertTrue(await self.db.scalar("SELECT 1 FROM rr_synced WHERE message_id = ?", (message_id,)))
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "reactionrole.create")
        self.assertEqual(body["rows"][0]["roles"], [str(self.red.id)])

    async def test_create_with_buttons_posts_them_with_the_message(self):
        client = await self.client()
        response = await client.post(self.path("/reactionroles"), json=self.form(mode="buttons", rows=[{"emoji": RED, "roles": [str(self.red.id)], "label": "Reds", "style": "danger"}]))
        self.assertEqual(response.status, 200)
        message_id = int((await response.json())["message_id"])
        self.assertEqual(self.ops.messages[message_id]["components"], [{"type": "button", "custom_id": "rr:b:0", "label": "Reds", "emoji": RED, "style": "danger"}])
        self.assertEqual(self.ops.messages[message_id].get("reactions", []), [])

    async def test_create_on_an_existing_message_by_id_and_by_latest(self):
        first = await self.ops.send_message(self.channel.id, {"content": "first", "embeds": [], "buttons": []})
        client = await self.client()
        response = await client.post(self.path("/reactionroles"), json={"create_mode": "id", "channel_id": str(self.channel.id), "message_id": str(first["id"]), "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]})
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(await self.rr.get(first["id"]))
        second = await self.ops.send_message(self.channel.id, {"content": "second", "embeds": [], "buttons": []})
        response = await client.post(self.path("/reactionroles"), json={"create_mode": "latest", "channel_id": str(self.channel.id), "rows": [{"emoji": BLUE, "roles": [str(self.blue.id)]}]})
        self.assertEqual(response.status, 200)
        self.assertEqual(int((await response.json())["message_id"]), second["id"])
        response = await client.post(self.path("/reactionroles"), json={"create_mode": "id", "channel_id": str(self.channel.id), "message_id": str(first["id"]), "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]})
        self.assertEqual(response.status, 409)
        await self.rr.settle()

    async def test_another_bots_message_takes_reactions_only(self):
        theirs = await self.ops.send_message(self.channel.id, {"content": "theirs", "embeds": [], "buttons": []})
        self.ops.messages[theirs["id"]]["mine"] = False
        client = await self.client()
        form = {"create_mode": "id", "channel_id": str(self.channel.id), "message_id": str(theirs["id"]), "rows": [{"emoji": RED, "roles": [str(self.red.id)]}]}
        response = await client.post(self.path("/reactionroles"), json={**form, "mode": "buttons"})
        self.assertEqual(response.status, 400)
        self.assertIn("mode", (await response.json())["fields"])
        response = await client.post(self.path("/reactionroles"), json=form)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["mine"])
        self.assertEqual(self.ops.messages[theirs["id"]]["payload"]["content"], "theirs")
        message = await (await client.get(self.path(f"/reactionroles/{theirs['id']}/message"))).json()
        self.assertTrue(message["readonly"])
        response = await client.patch(self.path(f"/reactionroles/{theirs['id']}"), json={"rows": form["rows"], "payload": {"content": "changed", "embeds": [], "buttons": []}})
        self.assertEqual(response.status, 400)
        response = await client.patch(self.path(f"/reactionroles/{theirs['id']}"), json={"rows": form["rows"], "type": "unique"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.rr.get(theirs["id"])).kind, "unique")
        await self.rr.settle()

    async def test_lookup_says_who_wrote_the_message(self):
        mine = await self.ops.send_message(self.channel.id, {"content": "mine", "embeds": [], "buttons": []})
        client = await self.client()
        body = await (await client.get(self.path(f"/reactionroles/lookup?channel_id={self.channel.id}&message_id={mine['id']}"))).json()
        self.assertTrue(body["mine"])
        self.assertFalse(body["tracked"])
        body = await (await client.get(self.path(f"/reactionroles/lookup?channel_id={self.channel.id}&latest=1"))).json()
        self.assertEqual(int(body["message_id"]), mine["id"])
        response = await client.get(self.path(f"/reactionroles/lookup?channel_id={self.channel.id}&message_id=nonsense"))
        self.assertEqual(response.status, 400)
        response = await client.get(self.path(f"/reactionroles/lookup?channel_id={new_id()}&message_id={mine['id']}"))
        self.assertEqual(response.status, 400)
        response = await client.get(self.path(f"/reactionroles/lookup?channel_id={self.channel.id}&message_id={new_id()}"))
        self.assertEqual(response.status, 502)

    async def test_bad_forms_name_the_field(self):
        client = await self.client()
        cases = [
            ({"rows": []}, "rows"),
            ({"rows": [{"emoji": RED, "roles": []}]}, "rows.0.roles"),
            ({"rows": [{"emoji": "not an emoji", "roles": [str(self.red.id)]}]}, "rows.0.emoji"),
            ({"rows": [{"emoji": "", "roles": [str(self.red.id)]}]}, "rows.0.emoji"),
            ({"rows": [{"emoji": RED, "roles": [str(new_id())]}]}, "rows.0.roles"),
            ({"rows": [{"emoji": RED, "roles": [str(self.guild.default_role.id)]}]}, "rows.0.roles"),
            ({"rows": [{"emoji": RED, "roles": [str(self.red.id)]}, {"emoji": RED + "\ufe0f", "roles": [str(self.blue.id)]}]}, "rows.1.emoji"),
            ({"rows": [{"emoji": chr(0x1F600 + i), "roles": [str(self.red.id)]} for i in range(21)]}, "rows"),
            ({"type": "sideways"}, "type"),
            ({"mode": "smoke signals"}, "mode"),
            ({"type": "limit", "limit": 0}, "limit"),
            ({"type": "limit", "limit": 251}, "limit"),
            ({"type": "temp", "temp_seconds": 5}, "temp_seconds"),
            ({"whitelist": [str(new_id())]}, "whitelist"),
            ({"blacklist": "nope"}, "blacklist"),
            ({"channel_id": str(new_id())}, "channel_id"),
            ({"channel_id": "nope"}, "channel_id"),
            ({"create_mode": "sideways"}, "create_mode"),
            ({"payload": {"content": "", "embeds": [], "buttons": []}}, "payload"),
            ({"destruct_seconds": 5}, "destruct_seconds"),
        ]
        for extra, field in cases:
            with self.subTest(field=field, extra=str(extra)[:60]):
                response = await client.post(self.path("/reactionroles"), json=self.form(**extra))
                self.assertEqual(response.status, 400)
                self.assertIn(field, (await response.json())["fields"])
        self.assertEqual(await self.rr.for_guild(self.guild.id), [])

    async def test_too_many_buttons_counts_the_link_buttons(self):
        client = await self.client()
        rows = [{"emoji": chr(0x1F600 + i), "roles": [str(self.red.id)]} for i in range(25)]
        payload = {"content": "x", "embeds": [], "buttons": [{"label": "Rules", "url": "https://example.com"}]}
        response = await client.post(self.path("/reactionroles"), json=self.form(mode="buttons", rows=rows, payload=payload))
        self.assertEqual(response.status, 400)
        self.assertIn("rows", (await response.json())["fields"])

    async def test_a_message_with_too_much_text_is_refused(self):
        client = await self.client()
        response = await client.post(self.path("/reactionroles"), json=self.form(payload={"content": "x" * 2001, "embeds": [], "buttons": []}))
        self.assertEqual(response.status, 400)

    async def test_edit_changes_the_text_roles_and_reactions(self):
        client = await self.client()
        body = await (await client.post(self.path("/reactionroles"), json=self.form(rows=[{"emoji": RED, "roles": [str(self.red.id)]}, {"emoji": BLUE, "roles": [str(self.blue.id)]}]))).json()
        message_id = body["message_id"]
        response = await client.patch(self.path(f"/reactionroles/{message_id}"), json={
            "type": "unique", "mode": "reactions", "payload": {"content": "Edited", "embeds": [{"title": "New"}], "buttons": []},
            "rows": [{"emoji": BLUE, "roles": [str(self.blue.id), str(self.green.id)], "max": {str(self.green.id): 3}}, {"emoji": GREEN, "roles": [str(self.green.id)]}],
        })
        self.assertEqual(response.status, 200)
        stored = self.ops.messages[int(message_id)]
        self.assertEqual(stored["payload"]["content"], "Edited")
        self.assertEqual(stored["payload"]["embeds"][0]["title"], "New")
        self.assertEqual({r["emoji"] for r in stored["reactions"]}, {BLUE, GREEN})
        found = await self.rr.get(int(message_id))
        self.assertEqual(found.kind, "unique")
        self.assertEqual(found.options[0].limits, {self.green.id: 3})
        await self.rr.settle()

    async def test_edit_with_no_new_text_leaves_the_message_alone(self):
        client = await self.client()
        body = await (await client.post(self.path("/reactionroles"), json=self.form())).json()
        calls = []
        real = self.ops.edit_message

        async def noting(*args, **kwargs):
            calls.append(args)
            await real(*args, **kwargs)

        self.ops.edit_message = noting
        await client.patch(self.path(f"/reactionroles/{body['message_id']}"), json={"type": "verify", "rows": body["rows"]})
        self.assertEqual(calls, [])

    async def test_switching_to_buttons_from_the_form(self):
        client = await self.client()
        body = await (await client.post(self.path("/reactionroles"), json=self.form())).json()
        response = await client.patch(self.path(f"/reactionroles/{body['message_id']}"), json={"mode": "buttons", "rows": body["rows"]})
        self.assertEqual(response.status, 200)
        stored = self.ops.messages[int(body["message_id"])]
        self.assertEqual(stored["components"][0]["custom_id"], "rr:b:0")
        self.assertEqual(stored["reactions"], [])

    async def test_someone_ranking_below_a_role_cant_add_it(self):
        self.ops.member = lambda guild_id, user_id: {"id": user_id, "role_ids": [self.role_member.id]}
        client = await self.client()
        response = await client.post(self.path("/reactionroles"), json=self.form())
        self.assertEqual(response.status, 400)
        self.assertIn("rows.0.roles", (await response.json())["fields"])
        response = await client.post(self.path("/reactionroles/import"), json={"text": "{}"})
        self.assertEqual(response.status, 403)

    async def test_someone_ranking_below_a_role_can_still_edit_a_message_that_has_it(self):
        rr = await self.make((RED, self.red))
        self.ops.member = lambda guild_id, user_id: {"id": user_id, "role_ids": [self.role_member.id]}
        client = await self.client()
        rows = [{"emoji": RED, "roles": [str(self.red.id)]}]
        response = await client.patch(self.path(f"/reactionroles/{rr.message_id}"), json={"rows": rows, "payload": {"content": "new words", "embeds": [], "buttons": []}})
        self.assertEqual(response.status, 200)
        rows.append({"emoji": BLUE, "roles": [str(self.blue.id)]})
        response = await client.patch(self.path(f"/reactionroles/{rr.message_id}"), json={"rows": rows})
        self.assertEqual(response.status, 400)

    async def test_ids_from_another_server_reach_nothing(self):
        rr = await self.make((RED, self.red))
        await self.db.run("UPDATE rr_messages SET guild_id = ? WHERE message_id = ?", (new_id(), rr.message_id))
        await self.db.run("UPDATE rr_pairs SET guild_id = ? WHERE message_id = ?", (new_id(), rr.message_id))
        client = await self.client()
        self.assertEqual(await (await client.get(self.path("/reactionroles"))).json(), {"rr": [], "elevated_roles": False, "dangerous": list(module.DANGEROUS_PERMISSIONS)})
        for method, tail, body in (("get", "/message", None), ("patch", "", {"rows": []}), ("delete", "", None), ("post", "/fix", {}), ("post", "/move", {"channel_id": str(self.channel.id), "message_id": str(new_id())})):
            response = await getattr(client, method)(self.path(f"/reactionroles/{rr.message_id}{tail}"), **({"json": body} if body is not None else {}))
            self.assertEqual(response.status, 404, f"{method} {tail}")
        self.assertIsNotNone(await self.rr.get(rr.message_id))

    async def test_linking_from_the_form_rejects_another_servers_message(self):
        mine = await self.make((RED, self.red))
        theirs = await self.make((BLUE, self.blue))
        await self.db.run("UPDATE rr_messages SET guild_id = ? WHERE message_id = ?", (new_id(), theirs.message_id))
        client = await self.client()
        response = await client.patch(self.path(f"/reactionroles/{mine.message_id}"), json={"rows": [{"emoji": RED, "roles": [str(self.red.id)]}], "linked": [str(theirs.message_id)]})
        self.assertEqual(response.status, 400)
        self.assertIn("linked", (await response.json())["fields"])

    async def test_linking_and_unlinking_from_the_form(self):
        mine = await self.make((RED, self.red))
        other = await self.make((BLUE, self.blue))
        client = await self.client()
        rows = [{"emoji": RED, "roles": [str(self.red.id)]}]
        response = await client.patch(self.path(f"/reactionroles/{mine.message_id}"), json={"rows": rows, "linked": [str(other.message_id)]})
        self.assertEqual(response.status, 200)
        group = (await self.rr.get(mine.message_id)).group_id
        self.assertIsNotNone(group)
        self.assertEqual((await self.rr.get(other.message_id)).group_id, group)
        response = await client.patch(self.path(f"/reactionroles/{mine.message_id}"), json={"rows": rows, "linked": []})
        self.assertEqual(response.status, 200)
        self.assertIsNone((await self.rr.get(mine.message_id)).group_id)
        self.assertIsNone((await self.rr.get(other.message_id)).group_id)

    async def test_self_destruct_from_the_form(self):
        client = await self.client()
        body = await (await client.post(self.path("/reactionroles"), json=self.form(destruct_seconds=3600))).json()
        self.assertIsNotNone(await self.bot.timers.get_key("rr_destruct", str(body["message_id"])))
        await client.patch(self.path(f"/reactionroles/{body['message_id']}"), json={"rows": body["rows"], "destruct_seconds": None})
        self.assertIsNone(await self.bot.timers.get_key("rr_destruct", str(body["message_id"])))

    async def test_delete_leaves_the_message(self):
        rr = await self.make((RED, self.red))
        client = await self.client()
        self.assertEqual((await client.delete(self.path(f"/reactionroles/{rr.message_id}"))).status, 200)
        self.assertIsNone(await self.rr.get(rr.message_id, trashed=True))
        self.assertIn(rr.message_id, self.ops.messages)

    async def test_fix_and_move_from_the_form(self):
        rr = await self.make((RED, self.red))
        await self.ops.remove_reaction(rr.channel_id, rr.message_id, RED)
        client = await self.client()
        self.assertEqual((await client.post(self.path(f"/reactionroles/{rr.message_id}/fix"))).status, 200)
        self.assertTrue(self.ops.messages[rr.message_id]["reactions"][0]["me"])
        target = await self.ops.send_message(self.channel.id, {"content": "x", "embeds": [], "buttons": []})
        response = await client.post(self.path(f"/reactionroles/{rr.message_id}/move"), json={"channel_id": str(self.channel.id), "message_id": str(target["id"])})
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(await self.rr.get(target["id"]))
        response = await client.post(self.path(f"/reactionroles/{target['id']}/move"), json={"channel_id": str(new_id()), "message_id": str(rr.message_id)})
        self.assertEqual(response.status, 400)

    async def test_staff_cant_open_any_of_it(self):
        rr = await self.make((RED, self.red))
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/reactionroles"))).status, 403)
        self.assertEqual((await client.post(self.path("/reactionroles"), json=self.form())).status, 403)
        self.assertEqual((await client.patch(self.path(f"/reactionroles/{rr.message_id}"), json={})).status, 403)
        self.assertEqual((await client.delete(self.path(f"/reactionroles/{rr.message_id}"))).status, 403)
        self.assertEqual((await client.get(self.path("/reactionroles/lookup"))).status, 403)
        self.assertEqual((await client.post(self.path("/reactionroles/import"), json={})).status, 403)

    def export(self, *entries) -> dict:
        return {"is_patron": False, "position": 100, "rr": list(entries)}

    def entry(self, message_id, *rows, kind="normal", whitelist=(), blacklist=(), group=None, extra="{}") -> dict:
        return {
            "message_id": str(message_id),
            "extra": {"blacklist": [str(i) for i in blacklist], "extra": extra, "group_id": group, "guild_id": self.guild.id, "message_id": message_id, "type": kind, "whitelist": [str(i) for i in whitelist]},
            "messages": [{"channel_id": str(self.channel.id), "message_id": str(message_id), "reaction_emoji": emoji, "role_id": str(role.id)} for emoji, role in rows],
        }

    async def test_import_fills_the_tables_and_changes_nobodys_roles(self):
        emoji_id = "123456789012345678"
        first = await self.ops.send_message(self.channel.id, {"content": "rules", "embeds": [], "buttons": []})
        second = await self.ops.send_message(self.channel.id, {"content": "roles", "embeds": [], "buttons": []})
        third = await self.ops.send_message(self.channel.id, {"content": "limited", "embeds": [], "buttons": []})
        fourth = await self.ops.send_message(self.channel.id, {"content": "timed", "embeds": [], "buttons": []})
        for sent in (first, second, third, fourth):
            self.ops.messages[sent["id"]]["mine"] = False
        early = self.newcomer()
        self.ops.react_as(second["id"], f"<:shiny:{emoji_id}>", self.me.id)
        self.ops.react_as(second["id"], f"<:shiny:{emoji_id}>", early.id)
        data = self.export(
            self.entry(first["id"], (CHECK, self.verified), kind="verify"),
            self.entry(second["id"], (emoji_id, self.red), ("\N{BALLOT BOX WITH BALLOT}\ufe0f", self.blue), (emoji_id, self.green), whitelist=[self.verified.id], blacklist=[self.role_muted.id]),
            self.entry(third["id"], (RED, self.red), kind="limit 3", extra=json.dumps({str(self.red.id): 7})),
            self.entry(fourth["id"], (RED, self.red), kind="temp 3600"),
            self.entry(new_id(), (RED, self.red)),
            self.entry(second["id"], (RED, self.red)),
            {"message_id": "nope", "messages": []},
        )
        client = await self.client()
        response = await client.post(self.path("/reactionroles/import"), json={"text": json.dumps(data)})
        self.assertEqual(response.status, 200)
        result = await response.json()
        await self.rr.settle()
        self.assertEqual(result["imported"], 4)
        self.assertEqual(result["pairs"], 6)
        self.assertEqual(len(result["skipped"]), 3)
        verify = await self.rr.get(first["id"])
        self.assertEqual((verify.kind, verify.foreign), ("verify", True))
        roles = await self.rr.get(second["id"])
        self.assertEqual(roles.whitelist, [self.verified.id])
        self.assertEqual(roles.blacklist, [self.role_muted.id])
        self.assertEqual(roles.options[0].emoji, f"<:shiny:{emoji_id}>")
        self.assertEqual(roles.options[0].role_ids, [self.red.id, self.green.id])
        self.assertEqual(emoji_key(roles.options[1].emoji), "\N{BALLOT BOX WITH BALLOT}")
        limited = await self.rr.get(third["id"])
        self.assertEqual((limited.kind, limited.limit, limited.options[0].limits), ("limit", 3, {self.red.id: 7}))
        timed = await self.rr.get(fourth["id"])
        self.assertEqual((timed.kind, timed.temp_seconds), ("temp", 3600))
        self.assertNotIn(self.red, early.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ? AND user_id = ?", (second["id"], early.id)), 1)
        # A member picks a role the next moment, so the import really does work.
        member = self.newcomer(self.verified)
        event = SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=second["id"], user_id=member.id, emoji=discord.PartialEmoji(name="whatever", id=int(emoji_id)), member=member)
        await self.rr.on_raw_reaction_add(event)
        await self.rr.settle()
        self.assertIn(self.red, member.roles)
        self.assertIn(self.green, member.roles)

    async def test_import_refuses_what_it_cant_read(self):
        client = await self.client()
        self.assertEqual((await client.post(self.path("/reactionroles/import"), json={"text": "not json"})).status, 400)
        self.assertEqual((await client.post(self.path("/reactionroles/import"), json={"text": ""})).status, 400)
        self.assertEqual((await client.post(self.path("/reactionroles/import"), json={"text": "5"})).status, 400)


class RealLibraryTest(GatewayTest):
    """The whole path through discord.py, with real objects and only the network faked."""

    modules = [ReactionRoles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.rr = next(m for m in self.bot.modules if isinstance(m, ReactionRoles))
        self.urls: list[tuple[str, str]] = []
        real = self.net.http
        self.reactors: dict[str, list[dict]] = {}

        async def noting(route, **kwargs):
            self.urls.append((route.method, route.url))
            if route.method == "GET" and route.path.endswith("/reactions/{emoji}"):
                params = kwargs.get("params") or {}
                people = self.reactors.get("burst" if params.get("type") == 1 else "normal", [])
                return [p for p in people if not params.get("after") or int(p["id"]) > int(params["after"])][:params.get("limit", 25)]
            return await real(route, **kwargs)

        self.bot.http.request = noting
        self.red_role = self.role_member

    async def make(self, emoji: str, **fields) -> RR:
        rr = RR(0, self.guild_id, int(self.text["id"]), created_at=time.time(), options=[Option(0, emoji, emoji_key(emoji), [int(self.role_member["id"])])], **fields)
        sent = await self.bot.ops.send_message(rr.channel_id, {"content": "pick", "embeds": [], "buttons": []}, components=self.rr.render(rr))
        rr.message_id = sent["id"]
        await self.rr._save(rr, None, fresh=True)
        self.urls.clear()
        return rr

    def react(self, rr: RR, emoji: str, user_id: int) -> None:
        self.bot._connection.parse_message_reaction_add({
            "user_id": str(user_id), "channel_id": str(rr.channel_id), "message_id": str(rr.message_id), "guild_id": str(self.guild_id), "member": self.member_json[user_id],
            "emoji": {"id": None, "name": emoji}, "burst": False, "type": 0, "message_author_id": str(self.bot_id),
        })

    async def finish(self) -> None:
        await self.settle()
        await self.rr.settle()

    def base(self) -> str:
        return "https://discord.com/api/v10"

    async def test_a_verify_reaction_reaches_discord_as_a_role_and_a_removal(self):
        rr = await self.make(CHECK, kind="verify")
        self.react(rr, CHECK, self.mod_id)
        await self.finish()
        self.assert_no_errors()
        methods = [(m, u.replace(self.base(), "")) for m, u in self.urls]
        self.assertIn(("PUT", f"/guilds/{self.guild_id}/members/{self.mod_id}/roles/{self.role_member['id']}"), methods)
        removal = [u for m, u in methods if m == "DELETE" and "/reactions/" in u]
        self.assertEqual(len(removal), 1)
        self.assertTrue(removal[0].endswith(f"/{self.mod_id}"))

    async def test_the_bots_own_reaction_is_ignored(self):
        rr = await self.make(CHECK, kind="verify")
        self.react(rr, CHECK, self.bot_id)
        await self.finish()
        self.assertEqual(self.urls, [])

    async def test_a_removal_takes_the_role(self):
        rr = await self.make(RED)
        self.react(rr, RED, self.mod_id)
        await self.finish()
        self.bot._connection.parse_message_reaction_remove({"user_id": str(self.mod_id), "channel_id": str(rr.channel_id), "message_id": str(rr.message_id), "guild_id": str(self.guild_id), "emoji": {"id": None, "name": RED}, "burst": False, "type": 0})
        await self.finish()
        self.assert_no_errors()
        methods = [(m, u.replace(self.base(), "")) for m, u in self.urls]
        self.assertIn(("DELETE", f"/guilds/{self.guild_id}/members/{self.mod_id}/roles/{self.role_member['id']}"), methods)

    def press(self, message: dict, custom_id: str, kind: int, *, values=None) -> None:
        data = {"custom_id": custom_id, "component_type": kind}
        if values is not None:
            data["values"] = values
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=self.mod_id, message=message))

    async def test_a_button_click_survives_as_a_dynamic_item_and_answers_privately(self):
        rr = await self.make(RED, mode="buttons")
        message = self.net.messages[rr.message_id]
        self.assertEqual(message["components"][0]["components"][0]["custom_id"], "rr:b:0")
        self.assertEqual(message["components"][0]["components"][0]["label"], "Member")
        self.net.calls.clear()
        self.press(message, "rr:b:0", 2)
        await self.finish()
        self.assert_no_errors()
        self.assertTrue(any(m == "PUT" and u.endswith(f"/roles/{self.role_member['id']}") for m, u in self.urls))
        callback = next(body for method, path, body in self.net.calls if path.endswith("/callback"))
        self.assertEqual(callback["type"], 6)
        followup = next(body for method, path, body in self.net.calls if path == "/webhooks/{webhook_id}/{webhook_token}")
        self.assertEqual(followup["flags"] & 64, 64)
        self.assertIn("You now have", followup["content"])
        self.assertEqual(followup["allowed_mentions"]["parse"], [])

    async def test_buttons_and_menus_answer_after_a_restart(self):
        """Nothing about them lives in memory but the registered patterns, which load at every start."""
        button = await self.make(RED, mode="buttons")
        menu = await self.make(BLUE, mode="select")
        store = self.bot._connection._view_store
        store._views.clear()
        store._modals.clear()
        self.press(self.net.messages[button.message_id], "rr:b:0", 2)
        await self.finish()
        self.press(self.net.messages[menu.message_id], "rr:s:0", 3, values=["0"])
        await self.finish()
        self.assert_no_errors()
        added = [u for m, u in self.urls if m == "PUT" and u.endswith(f"/roles/{self.role_member['id']}")]
        removed = [u for m, u in self.urls if m == "DELETE" and u.endswith(f"/roles/{self.role_member['id']}")]
        self.assertEqual((len(added), len(removed)), (1, 1))

    async def test_a_click_on_someone_elses_message_with_the_same_id_finds_nothing(self):
        """The ID alone proves nothing. The message has to hold reaction roles."""
        plain = await self.bot.ops.send_message(int(self.text["id"]), {"content": "plain", "embeds": [], "buttons": []}, components=[{"type": "button", "custom_id": "rr:b:0", "label": "Sneaky", "style": "primary"}])
        self.urls.clear()
        self.press(self.net.messages[plain["id"]], "rr:b:0", 2)
        await self.finish()
        self.assert_no_errors()
        self.assertFalse(any(m == "PUT" and "/roles/" in u for m, u in self.urls))

    async def test_a_menu_pick_resets_the_menu_and_answers_privately(self):
        rr = await self.make(RED, mode="select", placeholder="Colors")
        message = self.net.messages[rr.message_id]
        menu = message["components"][0]["components"][0]
        self.assertEqual((menu["custom_id"], menu["placeholder"], menu["min_values"], menu["max_values"]), ("rr:s:0", "Colors", 1, 1))
        self.net.calls.clear()
        self.press(message, "rr:s:0", 3, values=["0"])
        await self.finish()
        self.assert_no_errors()
        callback = next(body for method, path, body in self.net.calls if path.endswith("/callback"))
        self.assertEqual(callback["type"], 7)
        self.assertEqual(callback["data"]["components"][0]["components"][0]["custom_id"], "rr:s:0")
        self.assertTrue(any(m == "PUT" and u.endswith(f"/roles/{self.role_member['id']}") for m, u in self.urls))

    async def test_an_edit_keeps_the_controls_and_can_replace_them(self):
        rr = await self.make(RED, mode="buttons")
        await self.bot.ops.edit_message(rr.channel_id, rr.message_id, {"content": "new words", "embeds": [], "buttons": [{"label": "Rules", "url": "https://example.com/rules"}]})
        message = self.net.messages[rr.message_id]
        self.assertEqual(message["content"], "new words")
        parts = [c for row in message["components"] for c in row["components"]]
        self.assertEqual([p.get("custom_id") for p in parts], ["rr:b:0", None])
        self.assertEqual(parts[1]["url"], "https://example.com/rules")
        await self.bot.ops.edit_message(rr.channel_id, rr.message_id, {"content": "x", "embeds": [], "buttons": []}, components=[])
        self.assertEqual(self.net.messages[rr.message_id]["components"], [])

    async def test_catching_up_reads_reactions_through_the_library(self):
        rr = await self.make(CHECK, kind="verify")
        self.net.messages[rr.message_id]["reactions"] = [{"emoji": {"id": None, "name": CHECK}, "count": 2, "me": True, "me_burst": False, "burst_colors": [], "count_details": {"normal": 2, "burst": 0}}]
        self.reactors["normal"] = [{"id": str(self.mod_id), "username": "mod", "discriminator": "0", "avatar": None}]
        await self.rr.reconcile_message(rr.message_id)
        await self.finish()
        self.assert_no_errors()
        self.assertTrue(any(m == "PUT" and f"/members/{self.mod_id}/roles/" in u for m, u in self.urls))

    async def test_every_page_of_reactions_is_read(self):
        """Discord can return a short page before the end, so only an empty page ends the walk."""
        rr = await self.make(RED)
        ids = sorted(gateway_id() for _ in range(250))
        self.reactors["normal"] = [{"id": str(i), "username": "x", "discriminator": "0", "avatar": None} for i in ids[:200]]
        self.reactors["burst"] = [{"id": str(i), "username": "x", "discriminator": "0", "avatar": None, "bot": True} for i in ids[200:]]
        people = await self.rr._reactors(rr, rr.options[0], self.bot_id)
        self.assertEqual(sorted(people), ids)
        self.assertTrue(people[ids[-1]])
        self.assertFalse(people[ids[0]])

    async def test_a_missing_message_counts_as_deleted(self):
        rr = await self.make(RED)
        del self.net.messages[rr.message_id]
        await self.rr.reconcile_message(rr.message_id)
        self.assert_no_errors()
        self.assertIsNone(await self.rr.get(rr.message_id))

    async def test_removing_and_clearing_reach_discord(self):
        rr = await self.make(RED)
        await self.bot.ops.remove_reaction(rr.channel_id, rr.message_id, RED)
        await self.bot.ops.remove_reaction(rr.channel_id, rr.message_id, RED, self.mod_id)
        await self.bot.ops.clear_reaction(rr.channel_id, rr.message_id, RED)
        await self.bot.ops.clear_reactions(rr.channel_id, rr.message_id)
        paths = [u.replace(self.base(), "") for m, u in self.urls if m == "DELETE"]
        self.assertEqual(len(paths), 4)
        self.assertTrue(paths[0].endswith("/@me"))
        self.assertTrue(paths[1].endswith(f"/{self.mod_id}"))
        self.assertTrue(paths[3].endswith("/reactions"))
        self.assert_no_errors()

    async def test_a_custom_emoji_is_sent_to_discord_by_name_and_id(self):
        emoji = f"<:party:{gateway_id()}>"
        rr = await self.make(emoji)
        await self.bot.ops.reaction_users(rr.channel_id, rr.message_id, emoji)
        got = [u for m, u in self.urls if m == "GET"]
        self.assertIn("party%3A", got[0])
        self.assertNotIn("%3Cparty", got[0])

    async def autocomplete(self, command: str, options: list[dict]) -> list[dict]:
        data = {"id": str(gateway_id()), "name": "reactionrole", "type": 1, "options": [{"name": command, "type": 1, "options": options}]}
        self.net.calls.clear()
        self.bot._connection.parse_interaction_create(self.interaction_payload(4, data, user_id=self.owner_id))
        await self.settle()
        self.assert_no_errors()
        callback = next(body for method, path, body in self.net.calls if path.endswith("/callback"))
        return callback["data"]["choices"]

    async def test_role_and_message_autocomplete(self):
        rr = await self.make(RED)
        choices = await self.autocomplete("remove", [{"name": "role", "type": 3, "value": "mem", "focused": True}])
        self.assertEqual([c["name"] for c in choices], ["Member"])
        choices = await self.autocomplete("normal", [{"name": "message_id", "type": 3, "value": "", "focused": True}])
        self.assertEqual(choices[0]["value"], str(rr.message_id))
        self.assertIn("general", choices[0]["name"])

    async def run_slash(self, command: str, options: list[dict], *, user_id: int | None = None) -> None:
        data = {"id": str(gateway_id()), "name": "reactionrole", "type": 1, "options": [{"name": command, "type": 1, "options": options}], "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.owner_id))
        await self.finish()
        self.assert_no_errors(command)

    async def test_add_by_slash_command_ends_with_a_reaction_on_discord(self):
        rr = await self.make(RED)
        await self.run_slash("add", [{"name": "message_id", "type": 3, "value": str(rr.message_id)}, {"name": "emoji", "type": 3, "value": BLUE}, {"name": "role", "type": 3, "value": self.role_mod["id"]}])
        self.assertTrue(any(m == "PUT" and "/reactions/" in u for m, u in self.urls))
        found = await self.rr.get(rr.message_id)
        self.assertEqual(found.options[1].role_ids, [int(self.role_mod["id"])])

    async def test_an_ordinary_member_is_refused_by_the_command_check(self):
        rr = await self.make(RED)
        await self.run_slash("normal", [{"name": "message_id", "type": 3, "value": str(rr.message_id)}], user_id=self.member_id)
        self.assertEqual((await self.rr.get(rr.message_id)).kind, "normal")

    async def test_every_reactionrole_command_succeeds_through_the_library(self):
        """Valid input on every slash command, which the smoke test's made-up values mostly don't reach."""
        role = self.role_mod["id"]
        rr = await self.make(RED)
        other = await self.make(BLUE)
        third = await self.make(GREEN)
        message = {"name": "message_id", "type": 3, "value": str(rr.message_id)}
        steps = [
            ("show", []),
            ("show", [message]),
            ("add", [message, {"name": "emoji", "type": 3, "value": CHECK}, {"name": "role", "type": 3, "value": role}, {"name": "channel", "type": 7, "value": self.text["id"]}]),
            ("addmany", [message, {"name": "pairs", "type": 3, "value": f"\N{WHITE MEDIUM STAR} {self.role_muted['id']}"}]),
            ("blacklist", [{"name": "choice", "type": 3, "value": "add"}, message, {"name": "roles", "type": 3, "value": "Muted"}]),
            ("blacklist", [{"name": "choice", "type": 3, "value": "show"}]),
            ("whitelist", [{"name": "choice", "type": 3, "value": "add"}, message, {"name": "roles", "type": 3, "value": "Muted"}]),
            ("whitelist", [{"name": "choice", "type": 3, "value": "clear"}, message]),
            ("selfdestruct", [message, {"name": "when", "type": 3, "value": "1h"}]),
            ("selfdestruct", [message, {"name": "when", "type": 3, "value": "off"}]),
            ("edit", [message, {"name": "message_string", "type": 3, "value": "Games | Pick your games"}]),
            ("color", [message, {"name": "color", "type": 3, "value": "FF0000"}]),
            ("channel", [{"name": "channel_name", "type": 3, "value": "color-roles"}]),
            ("fix", []),
            ("fix", [message]),
            ("modify", [{"name": "choice", "type": 3, "value": "unique"}, message]),
            ("modify", [{"name": "choice", "type": 3, "value": "limit"}, message, {"name": "value", "type": 3, "value": "2"}]),
            ("normal", [message]),
            ("temp", [message, {"name": "duration", "type": 3, "value": "1h"}]),
            ("limit", [message, {"name": "limit", "type": 4, "value": 3}]),
            ("normal", [message]),
            ("link", [{"name": "message_ids", "type": 3, "value": f"{rr.message_id} {other.message_id}"}]),
            ("links", []),
            ("unlink", [message]),
            ("maxroles", [message, {"name": "roles_and_limits", "type": 3, "value": "Muted 5"}]),
            ("maxroles", [message]),
            ("mode", [message, {"name": "mode", "type": 3, "value": "buttons"}]),
            ("mode", [message, {"name": "mode", "type": 3, "value": "select"}]),
            ("mode", [message, {"name": "mode", "type": 3, "value": "reactions"}]),
            ("aio", [{"name": "type", "type": 3, "value": "default"}, {"name": "color", "type": 3, "value": "FF0000"}, {"name": "text", "type": 3, "value": f"Title | Words {RED} Muted"}]),
            ("remove", [{"name": "role", "type": 3, "value": self.role_muted["id"]}, message]),
            ("purge", [{"name": "message_id", "type": 3, "value": str(other.message_id)}]),
        ]
        for command, options in steps:
            self.net.calls.clear()
            data = {"id": str(gateway_id()), "name": "reactionrole", "type": 1, "options": [{"name": command, "type": 1, "options": options}], "resolved": {"channels": {self.text["id"]: {"id": self.text["id"], "name": "general", "type": 0, "permissions": "0", "parent_id": None, "guild_id": str(self.guild_id), "nsfw": False, "position": 0, "rate_limit_per_user": 0, "topic": None, "flags": 0, "last_message_id": None}}}}
            self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
            await self.finish()
            self.assert_no_errors(command)
            replies = [body for method, path, body in self.net.calls if "/callback" in path or path.startswith("/webhooks/")]
            self.assertTrue(replies, command)
            failed = [body for body in replies if "\N{CROSS MARK}" in str((body.get("data") or body).get("content"))]
            self.assertEqual(failed, [], command)
        self.assertFalse(await self.rr.holds_message(self.guild_id, other.message_id))

    async def test_fixforeign_joins_a_reaction_through_the_library(self):
        rr = await self.make(RED)
        self.net.messages[rr.message_id]["reactions"] = [{"emoji": {"id": str(gateway_id()), "name": "theirs", "animated": False}, "count": 1, "me": False, "me_burst": False, "burst_colors": [], "count_details": {"normal": 1, "burst": 0}}]
        await self.run_slash("fixforeign", [{"name": "message_id", "type": 3, "value": str(rr.message_id)}])
        self.assertTrue(any(m == "PUT" and "theirs" in u for m, u in self.urls))

    async def test_moving_to_the_latest_message_through_the_library(self):
        rr = await self.make(RED)
        target = await self.bot.ops.send_message(rr.channel_id, {"content": "new", "embeds": [], "buttons": []})
        await self.run_slash("move", [{"name": "base_id", "type": 3, "value": str(rr.message_id)}, {"name": "target_id", "type": 3, "value": str(target["id"])}])
        self.assertTrue(await self.rr.holds_message(self.guild_id, target["id"]))
        self.assertFalse(await self.rr.holds_message(self.guild_id, rr.message_id))

    async def test_purging_the_whole_server_asks_first_through_the_library(self):
        rr = await self.make(RED)
        self.net.posted.clear()
        data = {"id": str(gateway_id()), "name": "reactionrole", "type": 1, "options": [{"name": "purge", "type": 1, "options": []}], "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
        await self.settle(0.3)
        prompt = self.net.posted[-1]
        buttons = prompt["components"][0]["components"]
        self.assertEqual([b["label"] for b in buttons], ["Yes, remove them all", "Cancel"])
        # Only the person who ran it can answer.
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, {"custom_id": buttons[0]["custom_id"], "component_type": 2}, user_id=self.admin_id, message=prompt))
        await self.settle(0.3)
        self.assertTrue(await self.rr.holds_message(self.guild_id, rr.message_id))
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, {"custom_id": buttons[0]["custom_id"], "component_type": 2}, user_id=self.owner_id, message=prompt))
        await self.finish()
        self.assert_no_errors()
        self.assertFalse(await self.rr.holds_message(self.guild_id, rr.message_id))

    async def test_setup_walks_through_on_the_real_library(self):
        data = {"id": str(gateway_id()), "name": "reactionrole", "type": 1, "options": [{"name": "setup", "type": 1, "options": []}], "resolved": {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.owner_id))
        await self.settle(0.3)
        for answer in ("here", "Colors | Pick one", "ff0000", f"{RED} Muted", "done", "unique", "buttons"):
            self.dispatch_message(user_id=self.owner_id, content=answer)
            await self.settle(0.3)
        await self.finish()
        self.assert_no_errors()
        rows = await self.rr.for_guild(self.guild_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].kind, rows[0].mode), ("unique", "buttons"))
        message = self.net.messages[rows[0].message_id]
        self.assertEqual(message["embeds"][0]["title"], "Colors")
        self.assertEqual(message["components"][0]["components"][0]["custom_id"], "rr:b:0")

    async def test_describe_counts_each_roles_members(self):
        info = self.bot.ops.describe_guild(self.guild_id)
        roles = {row["name"]: row for row in info["roles"]}
        self.assertEqual(roles["Moderator"]["members"], 1)
        self.assertEqual(roles["Admin"]["members"], 2)
        self.assertEqual(roles["@everyone"]["members"], self.guild.member_count)

    async def test_text_commands_parse_every_form(self):
        rr = await self.make(RED)
        for text in (f"!rr add {rr.message_id} {BLUE} Moderator", f"!rr show {rr.message_id}", "!rr", "!rr list", f"!rr unique {rr.message_id}", f"!rr bl {rr.message_id} Muted", f"!rr remove {rr.message_id} Moderator", f"!reactrole normal {rr.message_id}", f"!rr colour {rr.message_id} ff0000"):
            self.dispatch_message(user_id=self.owner_id, content=text)
            await self.finish()
            self.assert_no_errors(text)
        found = await self.rr.get(rr.message_id)
        self.assertEqual(found.blacklist, [int(self.role_muted["id"])])
        self.assertEqual([o.role_ids for o in found.options], [[int(self.role_member["id"])]])
        self.assertEqual(found.kind, "normal")


class WhenInDoubtBase(Base):
    """Whatever the doubt, nobody's roles change, the reaction stays, and staff hear about it in the log channel."""

    modules = [ReactionRoles, Logs]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.logs = self.cog(Logs)
        self.log_channel = FakeChannel(self.guild, "logs")
        await self.bot.settings.save(self.guild.id, "logging", {"default_channel": self.log_channel.id, "events": []})

    async def asyncTearDown(self) -> None:
        await self.logs.cog_unload()
        await super().asyncTearDown()

    def notices(self) -> list[str]:
        return [embed["description"] for channel_id, embed, _ in self.bot.logs.posted if channel_id == self.log_channel.id and embed["title"] == "Reaction roles need a look"]

    async def restart(self, **options) -> ReactionRoles:
        fresh = ReactionRoles(self.bot)
        await fresh.reconcile_all(**options)
        await fresh.settle()
        await self.rr.settle()
        return fresh

    async def holders(self, role, people) -> int:
        return sum(role in member.roles for member in people)

    async def on_record(self, rr: RR) -> int:
        return await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (rr.message_id,))

    def timers_of(self, kind: str):
        return self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = ?", (kind,))


class RestoreWindowTest(Base):
    async def test_a_catch_up_starting_during_a_restore_changes_nobodys_roles(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        await member.remove_roles(self.red)
        await self.rr.before_restore(self.guild.id)
        await self.db.run("DELETE FROM rr_seen")  # the file's rows are older
        await self.rr.reconcile_message(rr.message_id, thorough=True)
        await self.rr.settle()
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE message_id = ?", (rr.message_id,)), 1)


class CatchUpThatFailsTest(WhenInDoubtBase):
    async def test_a_failed_read_at_startup_tries_again_and_the_gate_still_opens(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        self.reacted(rr, CHECK, member)
        with mock.patch.object(self.ops, "fetch_message", side_effect=OpsError("Discord refused: 503")):
            await self.restart()
        self.assertNotIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [member.id])
        self.assertEqual(await self.timers_of("rr_sync"), 1)
        self.assertEqual(self.notices(), [])  # one bad read tells nobody
        await self.bot.timers.run_due(time.time() + 61)
        await self.rr.settle()
        self.assertIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_staff_hear_when_reads_keep_failing(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        self.reacted(rr, CHECK, member)
        with mock.patch.object(self.ops, "fetch_message", side_effect=OpsError("The bot can't read that channel's history.")):
            await self.restart()
            await self.bot.timers.run_due(time.time() + 61)
            await self.rr.settle()
        self.assertEqual(len(self.notices()), 1)
        self.assertIn(rr.url(), self.notices()[0])
        self.assertIn("nobody's roles changed", self.notices()[0])
        self.assertNotIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [member.id])

    async def test_a_failed_read_of_who_reacted_tries_again(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        self.reacted(rr, CHECK, member)
        with mock.patch.object(self.ops, "reaction_users", side_effect=OpsError("Discord refused: 503")):
            await self.restart()
        self.assertEqual(await self.timers_of("rr_sync"), 1)
        await self.bot.timers.run_due(time.time() + 61)
        await self.rr.settle()
        self.assertIn(self.verified, member.roles)

    async def test_it_stops_after_the_limit_and_the_next_start_looks_again(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        with mock.patch.object(self.ops, "fetch_message", side_effect=OpsError("Discord refused: 503")):
            await self.rr.reconcile_message(rr.message_id, tries=module.RETRY_LIMIT)
        self.assertEqual(await self.timers_of("rr_sync"), 0)

    async def test_a_server_back_from_an_outage_is_read_again(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        self.reacted(rr, CHECK, member)
        self.guild.unavailable = True
        with mock.patch.object(self.ops, "fetch_message", side_effect=AssertionError("read during an outage")):
            await self.rr.reconcile_all()
        self.assertNotIn(self.verified, member.roles)
        self.guild.unavailable = False
        await self.rr.on_guild_available(self.guild)
        await self.rr.settle()
        self.assertIn(self.verified, member.roles)

    async def test_an_outage_in_the_middle_of_a_session_gets_a_full_read(self):
        rr = await self.make((RED, self.red))
        stayed = self.newcomer()
        await self.add(rr, RED, stayed)
        await self.rr.on_guild_unavailable(self.guild)
        # one person came and one went, which leaves the count where it was
        came = self.newcomer()
        self.reacted(rr, RED, came)
        await self.rr.on_guild_available(self.guild)
        await self.rr.settle()
        self.assertIn(self.red, came.roles)

    async def test_a_server_that_was_never_away_isnt_read_twice(self):
        await self.make((CHECK, self.verified), kind="verify")
        with mock.patch.object(self.ops, "fetch_message", side_effect=AssertionError("read again")):
            await self.rr.on_guild_available(self.guild)
            await self.rr.settle()


class ClearDuringDowntimeTest(WhenInDoubtBase):
    async def test_a_small_clear_costs_nobody_a_role(self):
        rr = await self.make((RED, self.red))
        members = [self.newcomer() for _ in range(module.MASS_CLEAR)]
        for member in members:
            await self.add(rr, RED, member)
        await self.ops.clear_reactions(rr.channel_id, rr.message_id)
        await self.restart()
        self.assertEqual(await self.holders(self.red, members), module.MASS_CLEAR)
        self.assertEqual(await self.on_record(rr), 0)
        self.assertEqual(len(self.notices()), 1)
        self.assertIn("nobody lost a role", self.notices()[0])
        self.assertTrue(self.ops.messages[rr.message_id]["reactions"][0]["me"])

    async def test_a_clear_some_people_reacted_again_after_costs_nobody_a_role(self):
        rr = await self.make((RED, self.red))
        members = [self.newcomer() for _ in range(100)]
        for member in members:
            await self.add(rr, RED, member)
        await self.ops.clear_reactions(rr.channel_id, rr.message_id)
        for member in members[:60]:
            self.reacted(rr, RED, member)
        await self.restart()
        self.assertEqual(await self.holders(self.red, members), 100)
        self.assertEqual(await self.on_record(rr), 60)
        await self.restart()  # the record now matches, so a later start changes nothing either
        self.assertEqual(await self.holders(self.red, members), 100)

    async def test_people_who_took_their_own_reaction_off_still_lose_the_role(self):
        rr = await self.make((RED, self.red))
        stays, leaves = self.newcomer(), self.newcomer()
        await self.add(rr, RED, stays)
        await self.add(rr, RED, leaves)
        self.unreacted(rr, RED, leaves)
        await self.restart()
        self.assertIn(self.red, stays.roles)
        self.assertNotIn(self.red, leaves.roles)
        self.assertEqual(self.notices(), [])

    async def test_an_emoji_the_bot_cant_use_changes_no_roles_when_people_are_gone(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        await self.ops.clear_reactions(rr.channel_id, rr.message_id)
        with mock.patch.object(self.ops, "add_reaction", side_effect=OpsError("Unknown Emoji")):
            await self.restart()
        self.assertIn(self.red, member.roles)
        self.assertEqual(await self.on_record(rr), 1)  # it can't tell a clear from leaving, so the record stays too


class BlankAnswerTest(WhenInDoubtBase):
    def blank(self):
        real = self.ops.fetch_message

        async def fetch(channel_id, message_id):
            return {**await real(channel_id, message_id), "reactions": []}

        async def users(*args, **kwargs):
            return []

        return mock.patch.object(self.ops, "fetch_message", side_effect=fetch), mock.patch.object(self.ops, "reaction_users", side_effect=users)

    async def reactors_with_one_stripped(self, rr: RR, count: int):
        members = [self.newcomer() for _ in range(count)]
        for member in members:
            await self.add(rr, RED, member)
        await members[0].remove_roles(self.red)  # a moderator took it, and the reaction stays on the message
        for member in members:
            member.calls.clear()
        return members

    async def test_a_big_blank_answer_changes_nothing_and_the_record_survives(self):
        rr = await self.make((RED, self.red))
        members = await self.reactors_with_one_stripped(rr, 40)
        fetch, users = self.blank()
        with fetch, users:
            await self.restart()
        self.assertEqual(await self.on_record(rr), 40)
        self.assertEqual(await self.timers_of("rr_sync"), 1)
        await self.restart()  # Discord is healthy again
        self.assertNotIn(self.red, members[0].roles)
        self.assertEqual(await self.holders(self.red, members), 39)
        self.assertEqual([call for member in members for call in member.calls], [])

    async def test_a_small_blank_answer_costs_nobody_a_role(self):
        rr = await self.make((RED, self.red))
        members = await self.reactors_with_one_stripped(rr, 20)
        fetch, users = self.blank()
        with fetch, users:
            await self.restart()
        self.assertEqual(await self.holders(self.red, members), 19)
        self.assertEqual(await self.on_record(rr), 20)

    async def test_a_blank_answer_can_only_put_the_bots_own_reaction_back(self):
        rr = await self.make((RED, self.red))
        await self.reactors_with_one_stripped(rr, 3)
        fetch, users = self.blank()
        with fetch, users, mock.patch.object(self.ops, "remove_reaction", side_effect=AssertionError("took a reaction off")):
            await self.restart()

    async def test_a_crowd_with_no_record_goes_on_record_with_no_roles(self):
        rr = await self.make((RED, self.red))
        known = self.newcomer()
        await self.add(rr, RED, known)
        crowd = [self.newcomer() for _ in range(module.MASS_CLEAR + 1)]
        for member in crowd:
            self.reacted(rr, RED, member)
        await self.restart()
        self.assertEqual(await self.holders(self.red, crowd), 0)
        self.assertEqual(await self.on_record(rr), len(crowd) + 1)
        self.assertEqual(len(self.reactors(rr, RED)), len(crowd) + 1)
        self.assertEqual(len(self.notices()), 1)
        self.assertIn("nobody's roles changed", self.notices()[0])

    async def test_a_few_new_reactions_still_get_their_roles(self):
        rr = await self.make((RED, self.red))
        known = [self.newcomer() for _ in range(40)]
        for member in known:
            await self.add(rr, RED, member)
        new = [self.newcomer() for _ in range(module.MASS_CLEAR + 1)]
        for member in new:
            self.reacted(rr, RED, member)
        await self.restart()
        self.assertEqual(await self.holders(self.red, new), len(new))

    async def test_a_short_list_tells_staff(self):
        rr = await self.make((RED, self.red))
        members = [self.newcomer() for _ in range(40)]
        for member in members:
            await self.add(rr, RED, member)
        self.ops.react_as(rr.message_id, RED, self.newcomer().id)
        real = self.ops.reaction_users

        async def one_short_page(*args, **kwargs):
            return [] if kwargs.get("after") else (await real(*args, **kwargs))[:5]

        with mock.patch.object(self.ops, "reaction_users", side_effect=one_short_page):
            await self.restart()
        self.assertEqual(await self.holders(self.red, members), 40)
        self.assertIn("nobody lost a role", self.notices()[0])


class OldReactionsTest(WhenInDoubtBase):
    async def attach(self, kind: str, emoji: str, role, people) -> int:
        sent = await self.ops.send_message(self.channel.id, {"content": "old post", "embeds": [], "buttons": []})
        for member in people:
            self.ops.react_as(sent["id"], emoji, member.id)
        client = await self.client()
        response = await client.post(self.path("/reactionroles"), json={"create_mode": "id", "channel_id": str(self.channel.id), "message_id": str(sent["id"]), "type": kind, "rows": [{"emoji": emoji, "roles": [str(role.id)]}]})
        self.assertEqual(response.status, 200)
        await self.rr.settle()
        return sent["id"]

    async def test_a_drop_message_on_old_reactions_takes_nothing(self):
        holders = [self.newcomer(self.red) for _ in range(30)]
        message_id = await self.attach("drop", RED, self.red, holders)
        self.assertEqual(await self.holders(self.red, holders), 30)
        self.assertEqual(len(self.ops.messages[message_id]["reactors"][RED]), 30)
        self.assertEqual(len(self.notices()), 1)
        self.assertIn("Nobody's roles changed", self.notices()[0])
        await self.restart()
        self.assertEqual(await self.holders(self.red, holders), 30)

    async def test_a_verify_message_on_old_reactions_gives_nothing(self):
        people = [self.newcomer() for _ in range(30)]
        message_id = await self.attach("verify", CHECK, self.verified, people)
        self.assertEqual(await self.holders(self.verified, people), 0)
        self.assertEqual(len(self.ops.messages[message_id]["reactors"][CHECK]), 30)
        await self.restart()
        self.assertEqual(await self.holders(self.verified, people), 0)

    async def test_someone_with_an_old_reaction_can_pass_by_reacting_again(self):
        person = self.newcomer()
        message_id = await self.attach("verify", CHECK, self.verified, [person])
        rr = await self.rr.get(message_id)
        await self.remove(rr, CHECK, person)
        self.assertNotIn(self.verified, person.roles)
        await self.add(rr, CHECK, person)
        self.assertIn(self.verified, person.roles)
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_a_new_reaction_during_downtime_still_passes_the_gate(self):
        person = self.newcomer()
        message_id = await self.attach("verify", CHECK, self.verified, [person])
        rr = await self.rr.get(message_id)
        newcomer = self.newcomer()
        self.reacted(rr, CHECK, newcomer)
        await self.restart()
        self.assertIn(self.verified, newcomer.roles)
        self.assertNotIn(self.verified, person.roles)

    async def test_an_import_changes_nobodys_roles(self):
        sent = await self.ops.send_message(self.channel.id, {"content": "old post", "embeds": [], "buttons": []})
        person = self.newcomer()
        self.ops.react_as(sent["id"], CHECK, person.id)
        data = {"rr": [{"message_id": str(sent["id"]), "extra": {"type": "verify"}, "messages": [{"channel_id": str(self.channel.id), "reaction_emoji": CHECK, "role_id": str(self.verified.id)}]}]}
        await self.rr.import_rows(self.guild.id, data)
        await self.rr.settle()
        self.assertNotIn(self.verified, person.roles)
        self.assertIn(person.id, self.reactors(await self.rr.get(sent["id"]), CHECK))

    async def test_a_message_that_turns_verify_without_manage_messages_gives_nothing(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        await member.remove_roles(self.red)
        with mock.patch.object(self.ops, "clear_reaction", side_effect=OpsError("no")):
            ctx = self.context()
            await self.rr.rr_verify.callback(self.rr, ctx, str(rr.message_id))
            await self.rr.settle()
        self.assertNotIn(self.red, member.roles)


class BlockedGateTest(WhenInDoubtBase):
    async def test_a_gate_role_out_of_reach_keeps_the_reaction_and_tells_staff(self):
        high = self.guild.role("Gate", 50)
        rr = await self.make((CHECK, high), kind="verify")
        member = self.newcomer()
        await self.add(rr, CHECK, member)
        self.assertNotIn(high, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [member.id])
        self.assertEqual(len(self.notices()), 1)
        self.assertIn(rr.url(), self.notices()[0])
        high.position = 3  # staff fix it and run the command the notice names
        ctx = self.context()
        await self.rr.rr_fix.callback(self.rr, ctx, str(rr.message_id))
        await self.rr.settle()
        self.assertIn(high, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_a_refusal_from_discord_keeps_the_reaction_and_the_next_start_gives_the_role(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        real = member.add_roles

        async def forbidden(*roles, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Permissions")

        member.add_roles = forbidden
        await self.add(rr, CHECK, member)
        self.assertEqual(self.reactors(rr, CHECK), [member.id])
        self.assertIn("Manage Roles", self.notices()[0])
        member.add_roles = real
        await self.restart()
        self.assertIn(self.verified, member.roles)
        self.assertEqual(self.reactors(rr, CHECK), [])

    async def test_a_busy_gate_tells_staff_once(self):
        high = self.guild.role("Gate", 50)
        rr = await self.make((CHECK, high), kind="verify")
        for _ in range(5):
            await self.add(rr, CHECK, self.newcomer())
        self.assertEqual(len(self.notices()), 1)

    async def test_a_drop_out_of_reach_keeps_the_reaction(self):
        high = self.guild.role("High", 50)
        rr = await self.make((RED, high), kind="drop")
        member = self.newcomer(high)
        await self.add(rr, RED, member)
        self.assertIn(high, member.roles)
        self.assertEqual(self.reactors(rr, RED), [member.id])
        self.assertEqual(len(self.notices()), 1)

    async def test_a_member_who_cant_pick_it_up_still_loses_the_reaction(self):
        rr = await self.make((CHECK, self.red), kind="verify", whitelist=[self.verified.id])
        outsider = self.newcomer()
        await self.add(rr, CHECK, outsider)
        self.assertEqual(self.reactors(rr, CHECK), [])
        self.assertEqual(self.notices(), [])

    async def test_a_click_that_cant_work_says_so_and_tells_staff(self):
        high = self.guild.role("Gate", 50)
        rr = await self.make((CHECK, high), mode="buttons", kind="verify")
        text = await self.click(rr, self.newcomer(), 0)
        self.assertIn("can't hand out", text)
        self.assertEqual(len(self.notices()), 1)

    async def test_a_reaction_that_wont_come_off_goes_on_record_and_isnt_handled_twice(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        with mock.patch.object(self.ops, "remove_reaction", side_effect=OpsError("The bot can't remove reactions there. It needs Manage Messages in that channel.")):
            await self.add(rr, CHECK, member)
        self.assertIn(self.verified, member.roles)
        self.assertIn("Manage Messages", self.notices()[0])
        await member.remove_roles(self.verified)  # a moderator took it
        await self.restart()
        self.assertNotIn(self.verified, member.roles)


class RetryOrderTest(Base):
    async def test_a_waiting_retry_goes_when_the_member_changes_their_mind(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        real = member.add_roles

        async def down(*roles, **kwargs):
            raise discord.HTTPException(_FakeResponse(503), "down")

        member.add_roles = down
        await self.add(rr, RED, member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_retry'"), 1)
        member.add_roles = real
        await self.remove(rr, RED, member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_retry'"), 0)
        await self.bot.timers.run_due(time.time() + 3600)
        await self.rr.settle()
        self.assertNotIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE user_id = ?", (member.id,)), 0)

    async def test_a_waiting_removal_goes_when_the_member_reacts_again(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        await self.add(rr, RED, member)
        real = member.remove_roles

        async def down(*roles, **kwargs):
            raise discord.HTTPException(_FakeResponse(503), "down")

        member.remove_roles = down
        await self.remove(rr, RED, member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_retry'"), 1)
        member.remove_roles = real
        await self.add(rr, RED, member)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_retry'"), 0)
        self.assertIn(self.red, member.roles)


class EditWindowTest(Base):
    async def test_a_reaction_during_an_edit_still_finds_the_emoji(self):
        """Reads share the writer's connection, so one can land between the statements of an edit."""
        rr = await self.make((RED, self.red), (BLUE, self.blue))
        rr.options = [Option(0, RED, emoji_key(RED), [self.green.id])]
        seen = []
        real = self.db.many

        async def many(sql, rows):
            result = await real(sql, rows)
            current = await self.rr.get(rr.message_id)
            seen.append(sorted(o.key for o in current.options))
            return result

        with mock.patch.object(self.db, "many", side_effect=many):
            await self.rr._write(rr)
        self.assertTrue(all(emoji_key(RED) in keys for keys in seen), seen)
        final = await self.rr.get(rr.message_id)
        self.assertEqual([(o.key, o.role_ids) for o in final.options], [(emoji_key(RED), [self.green.id])])

    async def test_an_edit_replaces_every_field_of_a_row(self):
        rr = await self.make((RED, self.red))
        rr.options[0].label = "new label"
        rr.options[0].limits[self.red.id] = 3
        await self.rr._write(rr)
        final = await self.rr.get(rr.message_id)
        self.assertEqual(final.options[0].label, "new label")
        self.assertEqual(final.options[0].limits, {self.red.id: 3})


class MissingFromTheCacheTest(Base):
    async def test_a_temporary_role_ends_for_someone_the_cache_doesnt_have(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=60)
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.guild.members.remove(member)
        self.guild.fetch_member = mock.AsyncMock(return_value=member)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 61), 1)
        self.assertNotIn(self.red, member.roles)

    async def test_a_lookup_that_fails_keeps_the_timer(self):
        rr = await self.make((RED, self.red), kind="temp", temp_seconds=60)
        member = self.newcomer()
        await self.add(rr, RED, member)
        self.guild.members.remove(member)
        self.guild.fetch_member = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        await self.bot.timers.run_due(time.time() + 61)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'rr_temp'"), 1)
        self.assertEqual(self.reactors(rr, RED), [member.id])


class OutageThroughTheLibraryTest(GatewayTest):
    """READY listing the server as unavailable, the way the gateway does in an outage."""

    modules = [ReactionRoles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.rr = next(m for m in self.bot.modules if isinstance(m, ReactionRoles))
        self.net.keep_reactions = True
        channel_id = int(self.text["id"])
        self.message = self.net.message_json(channel_id, {"content": "rules"})
        self.message_id = int(self.message["id"])
        self.net.messages[self.message_id] = self.message
        rr = RR(self.message_id, self.guild_id, channel_id, kind="verify", created_at=time.time(), options=[Option(0, CHECK, emoji_key(CHECK), [int(self.role_member["id"])])])
        await self.rr._save(rr, None, fresh=True)
        # someone reacted while the bot was down
        self.net.reactors.setdefault((self.message_id, CHECK), {})[self.mod_id] = self.member_json[self.mod_id]["user"]
        self.message["reactions"] = [{"emoji": {"id": None, "name": CHECK}, "count": 2, "me": True, "me_burst": False, "burst_colors": [], "count_details": {"normal": 2, "burst": 0}}]

    def role_puts(self) -> int:
        return len([call for call in self.net.calls if call[0] == "PUT" and "/roles/" in call[1]])

    def guild_back(self) -> dict:
        members = list(self.member_json.values())
        return {
            "id": str(self.guild_id), "name": "Test Guild", "icon": None, "splash": None, "discovery_splash": None, "banner": None, "description": None,
            "owner_id": str(self.owner_id), "afk_channel_id": None, "afk_timeout": 300, "verification_level": 0, "default_message_notifications": 0,
            "explicit_content_filter": 0, "features": [], "mfa_level": 0, "premium_tier": 0, "premium_subscription_count": 0, "preferred_locale": "en-US",
            "nsfw_level": 0, "premium_progress_bar_enabled": False, "system_channel_id": None, "system_channel_flags": 0, "rules_channel_id": None,
            "public_updates_channel_id": None, "max_members": 500000, "vanity_url_code": None, "application_id": None,
            "joined_at": "2025-01-01T00:00:00+00:00", "large": False, "member_count": len(members), "unavailable": False,
            "roles": list(self.net.roles.values()), "members": members, "channels": list(self.net.channels.values()), "threads": [], "emojis": [], "stickers": [], "presences": [], "voice_states": [],
            "stage_instances": [], "guild_scheduled_events": [], "soundboard_sounds": [],
        }

    async def test_the_gate_opens_when_the_server_comes_back(self):
        state = self.bot._connection
        state.guild_ready_timeout = 0.01
        state.parse_ready({"v": 10, "user": self.bot_user, "guilds": [{"id": str(self.guild_id), "unavailable": True}], "session_id": "x", "resume_gateway_url": "wss://x", "application": {"id": str(self.bot_id), "flags": 0}})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if self.rr._reconciling is not None and self.rr._reconciling.done():
                break
        await self.rr.settle()
        self.assertEqual(self.role_puts(), 0)
        state.parse_guild_create(self.guild_back())
        await self.settle()
        await self.rr.settle()
        self.assertEqual(self.role_puts(), 1)
        self.assertNotIn(self.mod_id, self.net.reactors[(self.message_id, CHECK)])
        self.assert_no_errors()
