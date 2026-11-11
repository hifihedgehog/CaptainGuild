"""Which invite a join came through: read from the use counts, kept for a while, and never a queue of calls to Discord."""
import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from captainguild.core import invitewatch
from captainguild.core.invitewatch import KEEP_SECONDS, MAX_KEPT, MAX_WAITING, Invite, InviteWatch, sample

UTC = datetime.timezone.utc
MADE = datetime.datetime(2026, 11, 1, tzinfo=UTC)


def a_user(user_id, global_name, *, discriminator="0"):
    """An account as Discord describes an inviter: a username, and a global name when they chose one. `nick` is here to show nothing reads it."""
    return SimpleNamespace(id=user_id, name=(global_name or "plain").lower().replace(" ", "."), global_name=global_name, discriminator=discriminator, nick="Server Nickname", display_name="Server Nickname")


def an_invite(code, uses=0, *, max_uses=0, max_age=0, temporary=False, inviter=(555, "Grace"), channel=444, expires_at=None):
    who = a_user(*inviter) if inviter else None
    return SimpleNamespace(code=code, uses=uses, max_uses=max_uses, max_age=max_age, temporary=temporary, inviter=who, channel=SimpleNamespace(id=channel), created_at=MADE, expires_at=expires_at)


class Server:
    """A server as the watch reads it: its invites, its vanity link and whether the bot may read them."""

    def __init__(self, guild_id=1):
        self.id = guild_id
        self.features = []
        self.me = SimpleNamespace(id=9, name="Captain", global_name=None, discriminator="1234", display_name="Cap", guild_permissions=SimpleNamespace(manage_guild=True))
        self.listed = []
        self.vanity = None
        self.reads = 0
        self.gate = None
        self.error = None

    async def invites(self):
        self.reads += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return list(self.listed)

    async def vanity_invite(self):
        return self.vanity


def member(server, user_id=100):
    return SimpleNamespace(id=user_id, guild=server)


def refusal(status):
    response = SimpleNamespace(status=status, reason="no")
    return discord.Forbidden(response, "no") if status == 403 else discord.HTTPException(response, "no")


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.watch = InviteWatch()
        self.watch.settle = 0  # the tests that wait for joins to settle set their own
        self.server = Server()
        self.server.listed = [an_invite("alpha", 3), an_invite("beta", 7, inviter=(777, "Hopper"))]
        await self.watch.watch(self.server)


class AttributionTest(Case):
    async def test_the_invite_whose_count_rose_with_what_a_greeting_shows(self):
        self.server.listed = [an_invite("alpha", 4, max_uses=50, max_age=3600, temporary=True), an_invite("beta", 7)]
        found = await self.watch.used_by(member(self.server))
        self.assertEqual(found, Invite(code="alpha", uses=4, max_uses=50, max_age=3600, temporary=True, channel_id=444, created_at=MADE,
                                       expires_at=MADE + datetime.timedelta(hours=1), inviter_id=555, inviter_name="Grace", inviter_username="grace", inviter_global_name="Grace"))

    async def test_the_inviter_is_named_as_their_own_profile_names_them(self):
        """The global name, or the username when they set none, with the old tag where an account still has one. Never a name they go by in one server."""
        for who, wanted in [(a_user(1, "Grace Hopper"), ("Grace Hopper", "grace.hopper", "Grace Hopper")), (a_user(2, None), ("plain", "plain", "")),
                            (a_user(3, None, discriminator="5491"), ("plain#5491", "plain", "")), (a_user(4, "Grace Hopper", discriminator="5491"), ("Grace Hopper", "grace.hopper", "Grace Hopper"))]:
            self.server.listed = [an_invite("alpha", 3), an_invite("beta", 7)]
            await self.watch.watch(self.server)
            risen = an_invite("alpha", 4)
            risen.inviter = who
            self.server.listed = [risen, an_invite("beta", 7)]
            found = await self.watch.used_by(member(self.server, who.id))
            self.assertEqual((found.inviter_name, found.inviter_username, found.inviter_global_name), wanted, who)
            self.assertEqual(found.inviter_id, who.id)

    async def test_discord_s_own_end_date_is_used_when_it_gives_one(self):
        ends = MADE + datetime.timedelta(days=3)
        self.server.listed = [an_invite("alpha", 4, max_age=3600, expires_at=ends), an_invite("beta", 7)]
        self.assertEqual((await self.watch.used_by(member(self.server))).expires_at, ends)

    async def test_two_counts_rising_at_once_names_neither(self):
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 8)]
        self.assertIsNone(await self.watch.used_by(member(self.server)))

    async def test_no_count_rising_names_none(self):
        self.assertIsNone(await self.watch.used_by(member(self.server)))

    async def test_each_join_is_measured_from_the_one_before(self):
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertEqual((await self.watch.used_by(member(self.server, 100))).code, "alpha")
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 8)]
        self.assertEqual((await self.watch.used_by(member(self.server, 101))).code, "beta")
        self.assertIsNone(await self.watch.used_by(member(self.server, 102)))

    async def test_a_one_use_invite_that_vanished_as_it_was_used(self):
        self.server.listed = [an_invite("alpha", 3), an_invite("once", 0, max_uses=1)]
        await self.watch.watch(self.server)
        self.server.listed = [an_invite("alpha", 3)]
        found = await self.watch.used_by(member(self.server))
        self.assertEqual((found.code, found.uses, found.max_uses, found.inviter_name), ("once", 1, 1, "Grace"))

    async def test_an_invite_made_after_the_first_read(self):
        self.watch.created(SimpleNamespace(**vars(an_invite("fresh", 0)), guild=self.server))
        self.server.listed = [an_invite("alpha", 3), an_invite("beta", 7), an_invite("fresh", 1)]
        self.assertEqual((await self.watch.used_by(member(self.server))).code, "fresh")

    async def test_a_one_use_invite_made_and_used_between_two_reads(self):
        """Discord deletes it as it's used, so no read ever lists it. Only the event that announced it says it existed."""
        self.watch.created(SimpleNamespace(**vars(an_invite("once", 0, max_uses=1, inviter=(777, "Hopper"))), guild=self.server))
        found = await self.watch.used_by(member(self.server))
        self.assertEqual((found.code, found.uses, found.inviter_id), ("once", 1, 777))

    async def test_a_new_invite_nobody_announced_still_counts_from_zero(self):
        self.server.listed = [an_invite("alpha", 3), an_invite("beta", 7), an_invite("quiet", 1)]
        self.assertEqual((await self.watch.used_by(member(self.server))).code, "quiet")

    async def test_the_vanity_link_counts_and_has_no_inviter(self):
        self.server.features = ["VANITY_URL"]
        self.server.vanity = SimpleNamespace(code="harbor", uses=900)
        await self.watch.watch(self.server)
        self.server.vanity = SimpleNamespace(code="harbor", uses=901)
        found = await self.watch.used_by(member(self.server))
        self.assertEqual(found, Invite(code="harbor", uses=901))

    async def test_a_server_without_a_vanity_link_is_never_asked_for_one(self):
        self.server.vanity_invite = mock.AsyncMock(side_effect=AssertionError("asked for a vanity link"))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertEqual((await self.watch.used_by(member(self.server))).code, "alpha")

    async def test_an_invite_with_no_inviter(self):
        self.server.listed = [an_invite("alpha", 4, inviter=None), an_invite("beta", 7)]
        found = await self.watch.used_by(member(self.server))
        self.assertEqual((found.inviter_id, found.inviter_name, found.inviter_username, found.inviter_global_name), (None, "", "", ""))


class ReadingTest(Case):
    async def test_without_manage_server_nothing_is_read_and_the_server_is_noted(self):
        self.server.me.guild_permissions.manage_guild = False
        self.server.reads = 0
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertIsNone(await self.watch.used_by(member(self.server)))
        self.assertEqual(self.server.reads, 0)
        self.assertEqual(self.watch.blocked, {self.server.id})
        self.server.me.guild_permissions.manage_guild = True
        self.assertEqual((await self.watch.used_by(member(self.server, 101))).code, "alpha", "the counts kept from before the permission went still stand")
        self.assertEqual(self.watch.blocked, set())

    async def test_a_server_whose_own_member_is_not_known_yet_is_read_the_next_time(self):
        me, self.server.me = self.server.me, None
        self.server.reads = 0
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertIsNone(await self.watch.used_by(member(self.server)))
        self.assertEqual((self.server.reads, self.watch.blocked), (0, set()), "that's not the same as being refused")
        self.server.me = me
        self.assertEqual((await self.watch.used_by(member(self.server, 101))).code, "alpha")

    async def test_discord_refusing_notes_the_server_and_an_outage_does_not(self):
        self.server.error = refusal(403)
        self.assertIsNone(await self.watch.used_by(member(self.server)))
        self.assertEqual(self.watch.blocked, {self.server.id})
        self.watch.blocked.clear()
        self.server.error = refusal(503)
        self.assertIsNone(await self.watch.used_by(member(self.server, 101)))
        self.assertEqual(self.watch.blocked, set())

    async def test_a_failed_read_keeps_the_earlier_counts(self):
        self.server.error = refusal(503)
        await self.watch.used_by(member(self.server))
        self.server.error = None
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertEqual((await self.watch.used_by(member(self.server, 101))).code, "alpha")

    async def test_a_server_nobody_asked_about_is_never_read(self):
        other = Server(2)
        other.listed = [an_invite("alpha", 1)]
        self.assertIsNone(await self.watch.used_by(member(other)))
        self.assertEqual(other.reads, 0)
        self.assertFalse(self.watch.watching(other.id))
        self.assertTrue(self.watch.watching(self.server.id))

    async def test_stopping_forgets_the_counts_and_keeps_nothing_from_a_read_under_way(self):
        self.server.gate = asyncio.Event()
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        reading = asyncio.ensure_future(self.watch.used_by(member(self.server)))
        await asyncio.sleep(0)
        self.watch.stop(self.server.id)
        self.server.gate.set()
        await reading
        self.assertEqual((self.watch._counts, self.watch._known), ({}, {}))
        self.server.gate = None
        self.server.reads = 0
        self.assertIsNone(await self.watch.used_by(member(self.server, 101)))
        self.assertEqual(self.server.reads, 0)

    async def test_an_invite_made_in_a_server_nobody_asked_about_is_ignored(self):
        other = Server(2)
        self.watch.created(SimpleNamespace(**vars(an_invite("fresh", 0)), guild=other))
        self.watch.created(SimpleNamespace(**vars(an_invite("fresh", 0)), guild=None))
        self.assertEqual(set(self.watch._counts), {self.server.id})


class RaidTest(Case):
    async def test_reads_go_one_at_a_time_and_only_a_few_joins_wait(self):
        self.server.gate = asyncio.Event()
        self.server.reads = 0
        waiting = [asyncio.ensure_future(self.watch.used_by(member(self.server, 100 + number))) for number in range(MAX_WAITING)]
        await asyncio.sleep(0)
        self.assertEqual(self.server.reads, 1, "the others wait for the first read to finish")
        for number in range(50):
            self.assertIsNone(await asyncio.wait_for(self.watch.used_by(member(self.server, 500 + number)), 1), "past the few that wait, a join is answered at once")
        self.assertEqual(self.server.reads, 1)
        self.server.gate.set()
        await asyncio.gather(*waiting)
        self.assertEqual(self.server.reads, MAX_WAITING)
        self.assertEqual(self.watch._waiting, {})
        self.assertEqual(self.watch.kept(self.server.id, 549), (True, None), "the join that got no answer still has one kept, so its DM agrees with its welcome")

    async def test_a_read_that_fails_frees_its_place_in_line(self):
        self.server.error = RuntimeError("something else broke")
        with self.assertRaises(RuntimeError):
            await self.watch.used_by(member(self.server))
        self.assertEqual(self.watch._waiting, {})
        self.assertFalse(self.watch._lock(self.server.id).locked())


class KeepingTest(Case):
    async def test_an_answer_is_kept_ten_minutes(self):
        now = [1000.0]
        self.watch.clock = lambda: now[0]
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        found = await self.watch.used_by(member(self.server))
        self.assertEqual(self.watch.kept(self.server.id, 100), (True, found))
        now[0] += KEEP_SECONDS - 1
        self.assertEqual(self.watch.kept(self.server.id, 100), (True, found))
        now[0] += 1
        self.assertEqual(self.watch.kept(self.server.id, 100), (False, None))
        self.assertEqual(self.watch._answers, {})
        self.assertEqual(KEEP_SECONDS, 600)

    async def test_not_knowing_is_kept_too_and_someone_never_asked_about_has_nothing(self):
        await self.watch.used_by(member(self.server))
        self.assertEqual(self.watch.kept(self.server.id, 100), (True, None))
        self.assertEqual(self.watch.kept(self.server.id, 999), (False, None))

    async def test_a_member_who_leaves_is_forgotten(self):
        await self.watch.used_by(member(self.server))
        self.watch.forget(self.server.id, 100)
        self.assertEqual(self.watch.kept(self.server.id, 100), (False, None))
        self.watch.forget(self.server.id, 100)

    async def test_what_is_kept_has_a_ceiling(self):
        now = [0.0]
        self.watch.clock = lambda: now[0]
        for number in range(MAX_KEPT):
            self.watch._keep((1, number), None)
        now[0] = KEEP_SECONDS / 2
        self.watch._keep((1, MAX_KEPT), None)
        self.assertEqual(len(self.watch._answers), MAX_KEPT, "with none past its time, the oldest goes")
        self.assertNotIn((1, 0), self.watch._answers)
        now[0] = KEEP_SECONDS + 1
        self.watch._keep((1, MAX_KEPT + 1), None)
        self.assertEqual(set(self.watch._answers), {(1, MAX_KEPT), (1, MAX_KEPT + 1)}, "everything past its time goes first")


class ArrivalsTest(Case):
    """Joins are counted as they arrive, and a read can only pin a rise on a join when that join is the only one since the counts were last read."""

    def bot(self, user_id=200):
        return SimpleNamespace(id=user_id, guild=self.server, bot=True)

    async def test_the_one_join_since_the_last_read_is_told(self):
        self.watch.arrived(member(self.server))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertEqual((await self.watch.used_by(member(self.server))).code, "alpha")

    async def test_a_join_nobody_read_for_makes_the_next_answer_unknown_and_the_one_after_is_told_again(self):
        self.watch.arrived(member(self.server, 100))  # a join the greeting skipped: no read
        self.watch.arrived(member(self.server, 101))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertIsNone(await self.watch.used_by(member(self.server, 101)), "the rise could be either join's")
        self.assertEqual(self.watch.kept(self.server.id, 101), (True, None))
        self.watch.arrived(member(self.server, 102))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 8)]
        self.assertEqual((await self.watch.used_by(member(self.server, 102))).code, "beta", "the counts read for the join before are the new starting point")

    async def test_a_join_with_no_invite_just_ahead_of_another_is_unknown(self):
        # 100 came in with no invite, through Server Discovery. 101 used alpha a moment later, and alpha's count rose before 100's read while
        # 101's event was still on its way. The read waits for the joins right behind it, counts 101 too, and names nobody.
        self.assertEqual((invitewatch.SETTLE_SECONDS, InviteWatch().settle), (1.5, 1.5))
        self.watch.settle = 0.05
        self.watch.arrived(member(self.server, 100))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        reading = asyncio.ensure_future(self.watch.used_by(member(self.server, 100)))
        await asyncio.sleep(0)
        self.watch.arrived(member(self.server, 101))
        self.assertIsNone(await reading, "alpha is 101's, not 100's")
        self.assertIsNone(await self.watch.used_by(member(self.server, 101)), "and 101's rise was in the counts 100's read took in")
        self.watch.arrived(member(self.server, 102))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 8)]
        self.assertEqual((await self.watch.used_by(member(self.server, 102))).code, "beta", "a join alone is still told after the wait")

    async def test_a_join_s_read_waits_for_the_joins_right_behind_it(self):
        self.watch.settle = 0.05
        self.watch.arrived(member(self.server))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        reads = self.server.reads
        reading = asyncio.ensure_future(self.watch.used_by(member(self.server)))
        await asyncio.sleep(0.02)
        self.assertEqual(self.server.reads, reads, "no read yet")
        self.assertEqual((await reading).code, "alpha")
        self.assertEqual(self.server.reads, reads + 1)

    async def test_a_bot_is_never_counted_since_it_comes_in_without_an_invite(self):
        self.watch.arrived(self.bot())
        self.watch.arrived(member(self.server))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertEqual((await self.watch.used_by(member(self.server))).code, "alpha")

    async def test_a_read_for_a_bot_after_a_person_arrived_is_unknown(self):
        self.watch.arrived(member(self.server))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertIsNone(await self.watch.used_by(self.bot()), "the rise is the person's")

    async def test_a_person_s_invite_is_still_told_when_a_bot_s_read_comes_first(self):
        self.watch.arrived(self.bot())
        self.watch.arrived(member(self.server, 100))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]  # the person used alpha
        reads = self.server.reads
        self.assertIsNone(await self.watch.used_by(self.bot()), "a bot comes in without an invite")
        self.assertEqual(self.server.reads, reads, "so its answer takes no read, and leaves the counts and the tally as they were")
        self.assertEqual((await self.watch.used_by(member(self.server, 100))).code, "alpha")

    async def test_a_bot_is_never_given_an_invite(self):
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]  # a rise from a join whose event hasn't come yet
        self.assertIsNone(await self.watch.used_by(self.bot()))
        self.watch.arrived(member(self.server, 100))
        self.assertEqual((await self.watch.used_by(member(self.server, 100))).code, "alpha", "the rise stays for that person's read")

    async def test_a_failed_read_keeps_the_joins_it_was_reading_for(self):
        self.watch.arrived(member(self.server, 100))
        self.server.error = refusal(503)
        self.assertIsNone(await self.watch.used_by(member(self.server, 100)))
        self.server.error = None
        self.watch.arrived(member(self.server, 101))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]  # the first join used alpha, and the second came with no invite
        self.assertIsNone(await self.watch.used_by(member(self.server, 101)), "two joins since the counts were last read, so alpha's rise is nobody's to claim")

    async def test_reading_every_count_again_starts_the_tally_over(self):
        self.watch.arrived(member(self.server, 100))
        self.watch.arrived(member(self.server, 101))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        await self.watch.watch(self.server)
        self.watch.arrived(member(self.server, 102))
        self.server.listed = [an_invite("alpha", 5), an_invite("beta", 7)]
        self.assertEqual((await self.watch.used_by(member(self.server, 102))).code, "alpha")

    async def test_a_full_read_that_fails_leaves_the_tally_as_it_was(self):
        self.watch.arrived(member(self.server, 100))
        self.server.error = refusal(503)
        await self.watch.watch(self.server)
        self.server.error = None
        self.watch.arrived(member(self.server, 101))
        self.server.listed = [an_invite("alpha", 4), an_invite("beta", 7)]
        self.assertIsNone(await self.watch.used_by(member(self.server, 101)))

    async def test_a_server_that_stops_being_watched_during_a_failed_read_keeps_no_tally(self):
        gate = asyncio.Event()
        self.server.gate = gate
        self.watch.arrived(member(self.server, 100))
        reading = asyncio.ensure_future(self.watch.used_by(member(self.server, 100)))
        await asyncio.sleep(0.01)
        self.watch.stop(self.server.id)
        self.server.error = refusal(503)
        gate.set()
        await reading
        self.assertEqual(self.watch._arrived, {})

    async def test_a_join_past_the_waiting_line_stays_counted(self):
        gate = asyncio.Event()
        self.server.gate = gate
        joins = [member(self.server, 100 + number) for number in range(MAX_WAITING + 1)]
        for person in joins:
            self.watch.arrived(person)
        waiting = [asyncio.ensure_future(self.watch.used_by(person)) for person in joins]
        await asyncio.sleep(0.01)
        gate.set()
        answers = await asyncio.gather(*waiting)
        self.assertEqual(answers, [None] * len(joins), "with five arrivals and one read at a time, none of them can be told apart")

    async def test_a_server_nobody_asked_about_counts_nothing_and_stopping_forgets_the_tally(self):
        self.watch.arrived(member(Server(2)))
        self.assertEqual(self.watch._arrived, {})
        self.watch.arrived(member(self.server))
        self.assertEqual(self.watch._arrived, {self.server.id: 1})
        self.watch.stop(self.server.id)
        self.assertEqual(self.watch._arrived, {})


class SampleTest(unittest.TestCase):
    def test_a_preview_gets_an_invite_made_by_the_bot(self):
        server = Server()
        made = sample(server, SimpleNamespace(id=444))
        self.assertEqual((made.code, made.uses, made.channel_id, made.inviter_id, made.inviter_name, made.inviter_username, made.max_uses, made.expires_at),
                         ("sample", 1, 444, 9, "Captain#1234", "Captain", 0, None))
        self.assertLess(made.created_at, datetime.datetime.now(UTC))
        bare = sample(SimpleNamespace())
        self.assertEqual((bare.code, bare.channel_id, bare.inviter_id, bare.inviter_name), ("sample", None, None, ""))


class RuleTest(unittest.TestCase):
    def test_it_reads_the_rule_the_statistics_pack_reads(self):
        from captainguild.core.stats import invites as rules
        self.assertIs(invitewatch.attribute, rules.attribute)
        self.assertIs(invitewatch.InviteState, rules.InviteState)
        self.assertEqual(invitewatch.VANITY_PREFIX, rules.VANITY_PREFIX)
