"""The member queue behind reaction roles: spam costs little, a rush can't starve anyone, and one member's events stay in order."""
import asyncio
import re
import time
from unittest import mock

from captainguild.core.ops import DiscordOps
from captainguild.modules import reactionroles as module
from captainguild.modules.reactionroles import RR, Option, ReactionRoles, Turns, Work, emoji_key
from tests.fakes import FakeGuild, FakeMember
from tests.gateway import GatewayTest
from tests.test_reactionroles import BLUE, CHECK, RED, Base


def counting(member: FakeMember) -> list[str]:
    """Every role API call this member costs."""
    calls: list[str] = []
    real_add, real_remove = member.add_roles, member.remove_roles

    async def add(*roles, **kwargs):
        calls.append("add")
        await real_add(*roles, **kwargs)

    async def remove(*roles, **kwargs):
        calls.append("remove")
        await real_remove(*roles, **kwargs)

    member.add_roles, member.remove_roles = add, remove
    return calls


class SpamTest(Base):
    async def test_a_hundred_toggles_cost_almost_no_role_calls(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        calls = counting(member)
        for _ in range(50):
            await self.rr.on_raw_reaction_add(self.reacted(rr, RED, member))
            await self.rr.on_raw_reaction_remove(self.unreacted(rr, RED, member))
        await self.rr.settle()
        self.assertEqual(calls, [])
        self.assertNotIn(self.red, member.roles)

    async def test_the_last_toggle_is_what_counts(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        calls = counting(member)
        for _ in range(5):
            await self.rr.on_raw_reaction_add(self.reacted(rr, RED, member))
            await self.rr.on_raw_reaction_remove(self.unreacted(rr, RED, member))
        await self.rr.on_raw_reaction_add(self.reacted(rr, RED, member))
        await self.rr.settle()
        self.assertEqual(calls, ["add"])
        self.assertIn(self.red, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE user_id = ?", (member.id,)), 1)

    async def test_adding_a_role_someone_holds_makes_no_call(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer(self.red)
        calls = counting(member)
        await self.add(rr, RED, member)
        self.assertEqual(calls, [])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM rr_seen WHERE user_id = ?", (member.id,)), 1)

    async def test_removing_a_role_nobody_holds_makes_no_call(self):
        rr = await self.make((RED, self.red))
        member = self.newcomer()
        calls = counting(member)
        await self.remove(rr, RED, member)
        self.assertEqual(calls, [])

    async def test_only_the_missing_roles_on_a_row_are_added(self):
        rr = await self.make((RED, [self.red, self.blue]))
        member = self.newcomer(self.red)
        ids = []
        real = member.add_roles

        async def add(*roles, **kwargs):
            ids.extend(role.id for role in roles)
            await real(*roles, **kwargs)

        member.add_roles = add
        await self.add(rr, RED, member)
        self.assertEqual(ids, [self.blue.id])

    async def test_a_gate_reaction_taken_off_before_its_turn_still_counts(self):
        rr = await self.make((CHECK, self.verified), kind="verify")
        member = self.newcomer()
        await self.rr.on_raw_reaction_add(self.reacted(rr, CHECK, member))
        await self.rr.on_raw_reaction_remove(self.unreacted(rr, CHECK, member))
        await self.rr.settle()
        self.assertIn(self.verified, member.roles)

    async def test_identical_clicks_waiting_share_one_answer(self):
        rr = await self.make((RED, self.red), mode="buttons")
        member = self.newcomer()
        calls = counting(member)
        gate = asyncio.Event()
        hold = self.newcomer()

        async def held(*roles, **kwargs):
            await gate.wait()

        hold.add_roles = held
        loop = asyncio.get_running_loop()
        futures = [loop.create_future() for _ in range(5)]
        self.rr._enqueue(self.guild.id, member.id, Work("added", rr.message_id, key="unrelated"))
        for future in futures:
            self.rr._enqueue(self.guild.id, member.id, Work("click", rr.message_id, slots=[0], member=member, done=[future]))
        gate.set()
        outcomes = await asyncio.gather(*futures)
        await self.rr.settle()
        self.assertEqual(calls, ["add"])
        self.assertTrue(all(outcome is outcomes[0] for outcome in outcomes))
        self.assertIn(self.red, member.roles)

    async def test_a_queue_has_a_bound_and_what_it_turned_away_is_read_back(self):
        rr = await self.make((RED, self.red))
        others = [await self.make((BLUE, self.blue)) for _ in range(3)]
        member = self.newcomer()
        with mock.patch.object(module, "MAX_QUEUED", 3):
            for other in others:
                self.rr._enqueue(self.guild.id, member.id, Work("removed", other.message_id, key=emoji_key(BLUE)))
            event = self.reacted(rr, RED, member)
            await self.rr.on_raw_reaction_add(event)
            self.assertEqual(len(self.rr._queues[(self.guild.id, member.id)].items), 3)
            await self.rr.settle()
        self.assertIn(self.red, member.roles, "the catch-up after the queue ran found the reaction it had turned away")

    async def test_a_click_past_the_bound_gets_an_answer(self):
        rr = await self.make((RED, self.red), mode="buttons")
        member = self.newcomer()
        with mock.patch.object(module, "MAX_QUEUED", 1):
            self.rr._enqueue(self.guild.id, member.id, Work("removed", rr.message_id, key="other"))
            future = asyncio.get_running_loop().create_future()
            self.rr._enqueue(self.guild.id, member.id, Work("click", rr.message_id, slots=[0], member=member, done=[future]))
            self.assertIn("a lot at once", future.result().note)
            await self.rr.settle()
        self.assertNotIn(self.red, member.roles)


class PacingTest(Base):
    async def test_a_member_past_the_burst_waits_and_their_events_merge_meanwhile(self):
        rr = await self.make((RED, self.red), mode="buttons")
        member = self.newcomer()
        calls = counting(member)
        slept = []
        real_sleep = asyncio.sleep

        async def sleep(seconds, *args, **kwargs):
            if seconds >= 1:
                slept.append(seconds)
                self.rr._paced.clear()
                seconds = 0
            await real_sleep(seconds, *args, **kwargs)

        with mock.patch.object(module.asyncio, "sleep", sleep):
            for _ in range(module.MEMBER_ROLE_BURST + 1):
                await self.click(rr, member, 0)
        self.assertEqual(len(calls), module.MEMBER_ROLE_BURST + 1)
        self.assertEqual(len(slept), 1)
        self.assertLessEqual(slept[0], module.MEMBER_ROLE_GAP)

    async def test_a_member_under_the_burst_never_waits(self):
        rr = await self.make((RED, self.red), mode="buttons")
        member = self.newcomer()
        with mock.patch.object(module.asyncio, "sleep", side_effect=AssertionError("it waited")):
            for _ in range(module.MEMBER_ROLE_BURST):
                await self.click(rr, member, 0)

    async def test_waiting_out_the_pace_holds_no_turn(self):
        rr = await self.make((RED, self.red))
        spammer = self.newcomer()
        self.rr._paced[(self.guild.id, spammer.id)] = time.monotonic() + 3600
        self.rr._enqueue(self.guild.id, spammer.id, Work("added", rr.message_id, key=emoji_key(RED), member=spammer))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.rr._turns.total, 0)
        self.assertNotIn(self.red, spammer.roles)
        other = self.newcomer()
        await self.add_without_settling(rr, RED, other)
        self.assertIn(self.red, other.roles)
        self.rr._paced.clear()
        self.rr._queues[(self.guild.id, spammer.id)].task.cancel()

    async def add_without_settling(self, rr: RR, emoji: str, member: FakeMember) -> None:
        await self.rr.on_raw_reaction_add(self.reacted(rr, emoji, member))
        await self.rr._queues[(self.guild.id, member.id)].task


class StarvationTest(Base):
    async def test_a_ninth_member_gets_a_turn_while_eight_spam(self):
        """Each spammer's next event waits behind the ninth, so the ninth is done before any spammer's second change."""
        rr = await self.make((RED, self.red), (BLUE, self.blue))
        self.rr._turns = Turns(1, 1)
        order: list[str] = []
        spammers = [self.newcomer() for _ in range(8)]

        async def spam(*roles, **kwargs):
            order.append("spam")
            await asyncio.sleep(0)

        for member in spammers:
            member.add_roles = spam
            self.rr._enqueue(self.guild.id, member.id, Work("added", rr.message_id, key=emoji_key(RED), member=member))
            self.rr._enqueue(self.guild.id, member.id, Work("added", rr.message_id, key=emoji_key(BLUE), member=member))
        ninth = self.newcomer()

        async def add(*roles, **kwargs):
            order.append("ninth")

        ninth.add_roles = add
        self.rr._enqueue(self.guild.id, ninth.id, Work("added", rr.message_id, key=emoji_key(RED), member=ninth))
        await self.rr.settle()
        self.assertEqual(order, ["spam"] * 8 + ["ninth"] + ["spam"] * 8)

    async def test_a_busy_server_doesnt_hold_up_another(self):
        rr = await self.make((RED, self.red))
        gate = asyncio.Event()
        rush = [self.newcomer() for _ in range(module.PARALLEL_MEMBERS * 2)]

        async def stuck(*roles, **kwargs):
            await gate.wait()

        for member in rush:
            member.add_roles = stuck
            self.rr._enqueue(self.guild.id, member.id, Work("added", rr.message_id, key=emoji_key(RED), member=member))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.rr._turns.running.get(self.guild.id), module.PARALLEL_PER_SERVER)
        self.assertLess(module.PARALLEL_PER_SERVER, module.PARALLEL_MEMBERS)
        other_guild = FakeGuild("Other")
        other_guild.me = FakeMember(other_guild, "Bot", other_guild.role("Bot", 9, "administrator", managed=True), bot=True)
        self.bot.fake_guilds.append(other_guild)
        await asyncio.wait_for(self.rr._turns.acquire(other_guild.id), 1)
        self.rr._turns.release(other_guild.id)
        gate.set()
        await self.rr.settle()


class TurnsTest(Base):
    async def test_servers_take_turns_in_rotation(self):
        turns = Turns(1, 1)
        await turns.acquire(1)
        order: list[str] = []

        async def wait(name: str, guild_id: int) -> None:
            await turns.acquire(guild_id)
            order.append(name)
            turns.release(guild_id)

        tasks = [asyncio.ensure_future(wait(name, guild_id)) for name, guild_id in (("a1", 1), ("a2", 1), ("a3", 1), ("b1", 2), ("c1", 3))]
        await asyncio.sleep(0)
        turns.release(1)
        await asyncio.gather(*tasks)
        self.assertEqual(order, ["a1", "b1", "c1", "a2", "a3"])

    async def test_a_server_at_its_share_is_skipped(self):
        turns = Turns(3, 1)
        await turns.acquire(1)
        waiting = asyncio.ensure_future(turns.acquire(1))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        await asyncio.wait_for(turns.acquire(2), 1)
        turns.release(2)
        self.assertFalse(waiting.done())
        turns.release(1)
        await asyncio.wait_for(waiting, 1)
        turns.release(1)
        self.assertEqual(turns.total, 0)

    async def test_a_canceled_waiter_gives_back_its_place(self):
        turns = Turns(1, 1)
        await turns.acquire(1)
        leaving = asyncio.ensure_future(turns.acquire(1))
        staying = asyncio.ensure_future(turns.acquire(1))
        await asyncio.sleep(0)
        leaving.cancel()
        await asyncio.gather(leaving, return_exceptions=True)
        turns.release(1)
        await asyncio.wait_for(staying, 1)
        turns.release(1)
        self.assertEqual(turns.total, 0)
        self.assertEqual(turns.waiting, {})

    async def test_a_waiter_canceled_as_its_turn_comes_gives_the_turn_on(self):
        turns = Turns(1, 1)
        await turns.acquire(1)
        leaving = asyncio.ensure_future(turns.acquire(1))
        staying = asyncio.ensure_future(turns.acquire(1))
        await asyncio.sleep(0)
        turns.release(1)  # the turn is granted, and the waiter hasn't woken yet
        leaving.cancel()
        await asyncio.gather(leaving, return_exceptions=True)
        await asyncio.wait_for(staying, 1)
        turns.release(1)
        self.assertEqual(turns.total, 0)


class CapTest(Base):
    async def capped(self, cap: int) -> RR:
        rr = RR(0, self.guild.id, self.channel.id, created_at=time.time())
        rr.options.append(Option(0, RED, emoji_key(RED), [self.red.id], {self.red.id: cap}))
        sent = await self.ops.send_message(self.channel.id, {"content": "x", "embeds": [], "buttons": []})
        rr.message_id = sent["id"]
        await self.rr._save(rr, None, fresh=True)
        return rr

    async def test_five_at_once_on_a_cap_of_one_gives_one_holder_before_the_gateway_says_so(self):
        rr = await self.capped(1)
        members = [self.newcomer() for _ in range(5)]
        granted = []

        for member in members:
            async def add(*roles, _who=member, **kwargs):
                await asyncio.sleep(0)
                granted.append(_who)  # the cache isn't told, as on Discord until the member-update event

            member.add_roles = add
        for member in members:
            await self.rr.on_raw_reaction_add(self.reacted(rr, RED, member))
        await self.rr.settle()
        self.assertEqual(len(granted), 1)
        self.assertEqual(len(self.reactors(rr, RED)), 1, "the others' reactions came off")

    async def test_the_count_is_freed_when_the_holder_drops_it(self):
        rr = await self.capped(1)
        first, second = self.newcomer(), self.newcomer()
        await self.add(rr, RED, first)
        await self.remove(rr, RED, first)
        await self.add(rr, RED, second)
        self.assertIn(self.red, second.roles)

    async def test_a_grant_the_gateway_has_shown_counts_once(self):
        rr = await self.capped(2)
        first, second, third = self.newcomer(), self.newcomer(), self.newcomer()
        await self.add(rr, RED, first)
        await self.add(rr, RED, second)
        await self.add(rr, RED, third)
        self.assertIn(self.red, second.roles)
        self.assertNotIn(self.red, third.roles)

    async def test_a_message_with_no_cap_takes_no_lock(self):
        rr = await self.make((RED, self.red))
        await self.add(rr, RED, self.newcomer())
        self.assertEqual(self.rr._cap_locks, {})


class ThroughTheLibraryTest(GatewayTest):
    """Two quick events, each with its own copy of the member made before any change, and the gateway's member update landing right after the HTTP answer."""

    modules = [ReactionRoles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.rr = next(m for m in self.bot.modules if isinstance(m, ReactionRoles))
        self.first = int(self.role_member["id"])
        self.second = int(self.role_muted["id"])
        self.user_id = self.mod_id
        self.held = list(self.member_json[self.user_id]["roles"])
        self.role_calls: list[tuple[str, int]] = []
        inner = self.net.http
        state = self.bot._connection

        async def request(route, **kwargs):
            result = await inner(route, **kwargs)
            found = re.search(r"/members/(\d+)/roles/(\d+)$", route.url)
            if found and route.method in ("PUT", "DELETE") and int(found.group(1)) == self.user_id:
                role_id = found.group(2)
                self.role_calls.append((route.method, int(role_id)))
                if route.method == "PUT" and role_id not in self.held:
                    self.held.append(role_id)
                elif route.method == "DELETE" and role_id in self.held:
                    self.held.remove(role_id)
                data = self.member_json[self.user_id]
                asyncio.get_running_loop().call_soon(state.parse_guild_member_update, {"guild_id": str(self.guild_id), "user": data["user"], "roles": list(self.held), "joined_at": data["joined_at"], "deaf": False, "mute": False, "flags": 0})
            return result

        self.bot.http.request = request

    async def pair(self, **fields) -> RR:
        rr = RR(0, self.guild_id, int(self.text["id"]), created_at=time.time(), options=[Option(0, RED, emoji_key(RED), [self.first]), Option(1, BLUE, emoji_key(BLUE), [self.second])], **fields)
        sent = await self.bot.ops.send_message(rr.channel_id, {"content": "pick", "embeds": [], "buttons": []}, components=self.rr.render(rr))
        rr.message_id = sent["id"]
        await self.rr._save(rr, None, fresh=True)
        return rr

    def react(self, rr: RR, emoji: str, user_id: int) -> None:
        self.bot._connection.parse_message_reaction_add({
            "user_id": str(user_id), "channel_id": str(rr.channel_id), "message_id": str(rr.message_id), "guild_id": str(self.guild_id), "member": self.member_json[user_id],
            "emoji": {"id": None, "name": emoji}, "burst": False, "type": 0, "message_author_id": str(self.bot_id),
        })

    def press(self, message: dict, custom_id: str) -> None:
        data = {"custom_id": custom_id, "component_type": 2}
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=self.user_id, message=message))

    async def finish(self) -> None:
        await self.settle()
        await self.rr.settle()

    def holds(self) -> set[int]:
        return {role.id for role in self.guild.get_member(self.user_id).roles}

    async def test_unique_keeps_one_role(self):
        rr = await self.pair(kind="unique")
        self.react(rr, RED, self.user_id)
        self.react(rr, BLUE, self.user_id)
        await self.finish()
        self.assert_no_errors()
        self.assertEqual(self.role_calls, [("PUT", self.first), ("DELETE", self.first), ("PUT", self.second)])
        self.assertNotIn(self.first, self.holds())
        self.assertIn(self.second, self.holds())

    async def test_binding_keeps_the_first_choice(self):
        rr = await self.pair(kind="binding")
        self.react(rr, RED, self.user_id)
        self.react(rr, BLUE, self.user_id)
        await self.finish()
        self.assert_no_errors()
        self.assertEqual(self.role_calls, [("PUT", self.first)])

    async def test_limit_holds_at_the_limit(self):
        rr = await self.pair(kind="limit", limit=1)
        self.react(rr, RED, self.user_id)
        self.react(rr, BLUE, self.user_id)
        await self.finish()
        self.assert_no_errors()
        self.assertEqual(self.role_calls, [("PUT", self.first)])

    async def test_a_double_click_goes_on_and_then_off(self):
        rr = await self.pair(mode="buttons")
        message = self.net.messages[rr.message_id]
        self.press(message, "rr:b:0")
        await self.finish()
        self.press(message, "rr:b:0")  # still carries the member as they were when the button was pressed
        await self.finish()
        self.assert_no_errors()
        self.assertEqual(self.role_calls, [("PUT", self.first), ("DELETE", self.first)])
        self.assertNotIn(self.first, self.holds())
