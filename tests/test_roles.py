import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import aiohttp
import discord

from captainguild.core.validate import Invalid
from captainguild.modules import roles as module
from captainguild.modules.reactionroles import ReactionRoles
from captainguild.modules.roles import BulkJob, Roles, find_role, find_roles, parse_signed, split_time
from tests.fakes import BotTest, FakeChannel, FakeMember, _FakeResponse
from tests.gateway import GatewayTest
from tests.gateway import new_id as gateway_id


class Base(BotTest):
    modules = [Roles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.roles = self.cog(Roles)
        self.pro = self.guild.role("Pro", 3)
        self.newbie = self.guild.role("Newbie", 2)
        self.spacing = mock.patch.object(module, "BULK_SPACING", 0)
        self.spacing.start()

    async def asyncTearDown(self) -> None:
        self.spacing.stop()
        await self.roles.cog_unload()
        await super().asyncTearDown()


class ParsingTest(Base):
    def test_find_role_reads_mentions_ids_and_names_in_any_case(self):
        self.assertIs(find_role(self.guild, self.pro.mention), self.pro)
        self.assertIs(find_role(self.guild, str(self.pro.id)), self.pro)
        self.assertIs(find_role(self.guild, "Pro"), self.pro)
        self.assertIs(find_role(self.guild, "pRO"), self.pro)
        self.assertIs(find_role(self.guild, '"Pro"'), self.pro)
        self.assertIsNone(find_role(self.guild, ""))
        self.assertIsNone(find_role(self.guild, "nothing"))

    def test_loose_matching_takes_the_start_of_a_name_only_when_one_fits(self):
        self.assertIs(find_role(self.guild, "Newb"), self.newbie)
        self.assertIsNone(find_role(self.guild, "Newb", loose=False))
        self.guild.role("Newbie Two", 2)
        self.assertIsNone(find_role(self.guild, "Newb"))

    def test_find_roles_handles_names_with_spaces(self):
        two = self.guild.role("Two Words", 2)
        found, missing = find_roles(self.guild, f"pro two words {self.newbie.mention} nothing")
        self.assertEqual(found, [self.pro, two, self.newbie])
        self.assertEqual(missing, ["nothing"])
        found, missing = find_roles(self.guild, "Two Words, Pro, nothing")
        self.assertEqual(found, [two, self.pro])
        self.assertEqual(missing, ["nothing"])

    def test_signed_lists(self):
        two = self.guild.role("Semi-Pro Two", 2)
        adds, removes, flips, missing = parse_signed(self.guild, f"+pro -newbie +Semi-Pro Two {self.role_member.mention}")
        self.assertEqual(adds, [self.pro, two, self.role_member])
        self.assertEqual(removes, [self.newbie])
        self.assertEqual((flips, missing), ([], []))
        adds, removes, flips, missing = parse_signed(self.guild, "pro +newbie")
        self.assertEqual((adds, removes, flips), ([self.newbie], [], [self.pro]))
        self.assertEqual(parse_signed(self.guild, "+nothing")[3], ["nothing"])

    def test_time_at_either_end(self):
        self.assertEqual(split_time("2h bot"), (7200, "bot"))
        self.assertEqual(split_time("Some Role 2h"), (7200, "Some Role"))
        self.assertEqual(split_time("3 days Some Role"), (259200, "Some Role"))
        self.assertEqual(split_time("no time here"), (None, "no time here"))


class LongTextTest(Base):
    """Any member can send text to /rank, and everything here runs on the event loop."""

    def counted_roles(self) -> list:
        reads = []
        guild = self.guild
        real = list(guild.roles)

        class Counting(type(guild)):
            @property
            def roles(self):
                reads.append(1)
                return real

            @roles.setter
            def roles(self, value):
                pass

        guild.__class__ = Counting
        self.addCleanup(setattr, guild, "__class__", Counting.__mro__[1])
        return reads

    def test_long_text_is_refused_before_any_matching(self):
        reads = self.counted_roles()
        text = " ".join(f"word{i}" for i in range(module.MAX_ROLE_WORDS + 1))
        with self.assertRaises(Invalid):
            parse_signed(self.guild, text)
        with self.assertRaises(Invalid):
            find_roles(self.guild, text)
        with self.assertRaises(Invalid):
            find_roles(self.guild, ",".join(["x"] * (module.MAX_ROLE_WORDS + 2)))
        self.assertEqual(reads, [])

    def test_one_read_of_the_role_list_for_a_whole_command(self):
        reads = self.counted_roles()
        adds, removes, flips, missing = parse_signed(self.guild, "+Pro -Newbie nothing here at all")
        self.assertEqual(len(reads), 1)
        self.assertEqual((adds, removes), ([self.pro], [self.newbie]))
        self.assertEqual(flips, [])

    def test_no_run_of_words_is_tried_past_the_longest_name(self):
        tried = []
        names = module.RoleNames(self.guild)
        real = names.find

        def find(text, *, loose=True):
            tried.append(text)
            return real(text, loose=loose)

        names.find = find
        self.assertEqual(names.longest, 1)
        find_roles(self.guild, "a b c d e f", names)
        self.assertEqual(tried, ["a", "b", "c", "d", "e", "f"])

    def test_a_name_with_spaces_still_wins_as_the_longest_run(self):
        long_name = self.guild.role("Very Long Role Name", 3)
        short = self.guild.role("Very", 3)
        found, missing = find_roles(self.guild, "Very Long Role Name Very")
        self.assertEqual(found, [long_name, short])
        self.assertEqual(missing, [])

    async def test_a_member_gets_the_sentence_from_rank_custom(self):
        ctx = self.context(self.member)
        with self.assertRaisesRegex(Invalid, "too many words"):
            await self.roles.rank_custom.callback(self.roles, ctx, roles=" ".join(["x"] * 500))

    def test_rank_commands_have_a_cooldown(self):
        for command in (self.roles.rank, self.roles.rank_custom, self.roles.rank_allranks, self.roles.ranks):
            self.assertIsNotNone(command._buckets._cooldown, command.name)
            self.assertEqual(command._buckets._cooldown.rate, module.RANK_USES)

    async def test_counting_ranks_counts_only_those_and_reads_no_role_objects(self):
        third = self.guild.role("Third", 3)
        FakeMember(self.guild, "one", self.pro, third)
        FakeMember(self.guild, "two", self.pro)
        counts = await module.rank_counts(self.guild, {self.pro.id})
        self.assertEqual(counts[self.pro.id], 2)
        self.assertNotIn(third.id, counts)


class BulkStartTest(Base):
    async def many(self, count: int = 3) -> list[FakeMember]:
        return [FakeMember(self.guild, f"m{i}") for i in range(count)]

    async def test_two_commands_at_once_start_one_job(self):
        await self.many()
        started = []
        real_run = self.roles._run_job

        async def run(guild_id, job):
            started.append(job)
            await real_run(guild_id, job)

        self.roles._run_job = run
        real_send = self.channel.send
        gate = asyncio.Event()

        async def waiting_send(*args, **kwargs):
            await gate.wait()
            return await real_send(*args, **kwargs)

        self.channel.send = waiting_send
        first_ctx, second_ctx = self.context(self.owner), self.context(self.owner)
        first = asyncio.ensure_future(self.roles._start_bulk(first_ctx, "add", "all", self.role_member))
        await asyncio.sleep(0)
        await asyncio.wait_for(self.roles._start_bulk(second_ctx, "remove", "all", self.role_member), 5)
        self.assertIn("already running", second_ctx.fails[0])
        gate.set()
        await first
        self.assertEqual([job.action for job in started], ["add"])
        await self.roles.jobs[self.guild.id].task

    async def test_nothing_to_do_gives_the_place_back(self):
        ctx = self.context(self.owner)
        await self.roles._start_bulk(ctx, "add", "bots", self.role_muted, None)
        for member in self.guild.members:
            if member.bot and self.role_muted not in member.roles:
                member.roles.append(self.role_muted)
        await asyncio.gather(*[job.task for job in self.roles.jobs.values() if job.task])
        ctx = self.context(self.owner)
        await self.roles._start_bulk(ctx, "add", "bots", self.role_muted, None)
        self.assertEqual(ctx.oks, ["Nobody needs that change."])
        self.assertEqual(self.roles.jobs, {})

    async def test_a_failure_while_starting_gives_the_place_back(self):
        await self.many()
        self.channel.send = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(403), "no"))
        with self.assertRaises(discord.HTTPException):
            await self.roles._start_bulk(self.context(self.owner), "add", "all", self.role_member)
        self.assertEqual(self.roles.jobs, {})

    async def test_a_large_job_asks_first(self):
        await self.many(3)
        ctx = self.context(self.owner)
        sent = {}

        async def send(content=None, **kwargs):
            sent.update(kwargs, content=content)
            kwargs["view"].answer = False
            kwargs["view"].stop()
            return object()

        ctx.send = send
        with mock.patch.object(module, "BULK_CONFIRM_OVER", 2):
            await self.roles._start_bulk(ctx, "add", "all", self.role_member)
        self.assertIn("Start it?", sent["content"])
        self.assertEqual(sent["view"].yes.label, "Yes, start it")
        self.assertEqual(ctx.fails, ["Left everything as it was."])
        self.assertEqual(self.roles.jobs, {})
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM role_bulk_jobs"), 0)

    async def test_a_yes_starts_it(self):
        await self.many(3)
        ctx = self.context(self.owner)

        async def send(content=None, **kwargs):
            kwargs["view"].answer = True
            kwargs["view"].stop()
            return object()

        ctx.send = send
        with mock.patch.object(module, "BULK_CONFIRM_OVER", 2):
            await self.roles._start_bulk(ctx, "add", "all", self.role_member)
        await self.roles.jobs[self.guild.id].task
        self.assertEqual(ctx.fails, [])

    async def test_the_ending_counts_who_was_on_the_rules_screen(self):
        FakeMember(self.guild, "waiting", pending=True)
        FakeMember(self.guild, "ready")
        ctx = self.context(self.owner)
        await self.roles._start_bulk(ctx, "add", "all", self.role_muted)
        job = self.roles.jobs[self.guild.id]
        await job.task
        text = self.channel.sent[-1].kwargs["embed"].description if "embed" in self.channel.sent[-1].kwargs else ""
        self.assertEqual(job.pending_skipped, 1)
        self.assertIn("1 member was skipped for being on the rules screen", text)


class StaleCancelTest(Base):
    async def test_an_old_post_ends_itself_and_leaves_the_job_alone(self):
        FakeMember(self.guild, "m")
        await self.roles._start_bulk(self.context(self.owner), "add", "all", self.role_muted)
        job = self.roles.jobs[self.guild.id]
        said = []

        async def edit_message(**kwargs):
            said.append(("edit", kwargs))

        async def followup(content, **kwargs):
            said.append(("followup", content))

        event = SimpleNamespace(guild_id=self.guild.id, user=self.owner, message=SimpleNamespace(id=123456), response=SimpleNamespace(edit_message=edit_message, send_message=mock.AsyncMock()), followup=SimpleNamespace(send=followup))
        await self.roles.cancel_clicked(event)
        self.assertIsNone(job.canceled_by)
        self.assertEqual(said[0], ("edit", {"view": None}))
        self.assertIn("job is over", said[1][1])
        await job.task

    async def test_its_own_post_still_cancels(self):
        FakeMember(self.guild, "m")
        await self.roles._start_bulk(self.context(self.owner), "add", "all", self.role_muted)
        job = self.roles.jobs[self.guild.id]
        event = SimpleNamespace(guild_id=self.guild.id, user=self.owner, message=SimpleNamespace(id=job.message_id), response=SimpleNamespace(send_message=mock.AsyncMock()))
        await self.roles.cancel_clicked(event)
        self.assertEqual(job.canceled_by, self.owner.display_name)
        await job.task


class TemporaryEndsTest(Base):
    async def test_above_the_bot_the_timer_stays_and_staff_hear(self):
        role = self.guild.role("T", 3)
        member = FakeMember(self.guild, "n")
        await self.roles._temp_change(self.context(self.owner), member, 60, [role], [])
        role.position = self.me.top_role.position + 2
        with mock.patch.object(module, "tell_staff") as told:
            await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'temprole'"), 1)
        self.assertIn(role, member.roles)
        told.assert_called_once()

    async def test_a_refusal_keeps_the_timer_and_a_retry_finishes_it(self):
        role = self.guild.role("T", 3)
        member = FakeMember(self.guild, "n")
        await self.roles._temp_change(self.context(self.owner), member, 60, [role], [])
        real = member.remove_roles
        member.remove_roles = mock.AsyncMock(side_effect=discord.Forbidden(_FakeResponse(403), "Missing Permissions"))
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'temprole'"), 1)
        member.remove_roles = real
        await self.bot.timers.run_due(time.time() + 7200)
        self.assertNotIn(role, member.roles)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'temprole'"), 0)

    async def test_a_member_who_no_longer_holds_it_ends_quietly(self):
        role = self.guild.role("T", 3)
        member = FakeMember(self.guild, "n")
        await self.roles._temp_change(self.context(self.owner), member, 60, [role], [])
        await member.remove_roles(role)
        role.position = self.me.top_role.position + 2
        await self.bot.timers.run_due(time.time() + 120)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'temprole'"), 0)

    async def test_taking_a_role_someone_doesnt_hold_is_refused(self):
        role = self.guild.role("VIP", 3)
        member = FakeMember(self.guild, "n")
        ctx = self.context(self.owner)
        await self.roles._temp_change(ctx, member, 60, [], [role])
        self.assertIn("doesn't have", ctx.fails[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'temprole'"), 0)

    async def test_giving_a_role_someone_holds_for_good_is_refused(self):
        role = self.guild.role("Perm", 3)
        member = FakeMember(self.guild, "n", role)
        ctx = self.context(self.owner)
        await self.roles._temp_change(ctx, member, 60, [role], [])
        self.assertIn("already has", ctx.fails[0])
        await self.bot.timers.run_due(time.time() + 120)
        self.assertIn(role, member.roles)

    async def test_running_it_again_moves_the_end_of_the_same_timer(self):
        role = self.guild.role("T", 3)
        member = FakeMember(self.guild, "n")
        await self.roles._temp_change(self.context(self.owner), member, 60, [role], [])
        ctx = self.context(self.owner)
        await self.roles._temp_change(ctx, member, 600, [role], [])
        self.assertEqual(ctx.fails, [])
        row = await self.bot.timers.get_key("temprole", f"{self.guild.id}:{member.id}:{role.id}")
        self.assertGreater(row["due_at"], time.time() + 500)


class RoleCommandsTest(Base):
    async def test_add_and_remove(self):
        ctx = self.context()
        await self.roles.role_add.callback(self.roles, ctx, self.member, role=self.pro)
        self.assertIn(self.pro, self.member.roles)
        self.assertIn("gave", ctx.oks[0])
        await self.roles.role_remove.callback(self.roles, ctx, self.member, role=self.pro)
        self.assertNotIn(self.pro, self.member.roles)

    async def test_hierarchy_refusals(self):
        high = self.guild.role("High", 20)
        mine = self.guild.role("Above Mod", 7)
        ctx = self.context()
        await self.roles.role_add.callback(self.roles, ctx, self.member, role=high)
        self.assertIn("bot's highest role", ctx.fails[0])
        ctx = self.context(author=self.mod)
        await self.roles.role_add.callback(self.roles, ctx, self.member, role=mine)
        self.assertIn("your highest role", ctx.fails[0])
        ctx = self.context(author=self.mod)
        await self.roles.role_add.callback(self.roles, ctx, self.admin, role=self.newbie)
        self.assertIn("at or above your highest role", ctx.fails[0])
        ctx = self.context()
        await self.roles.role_add.callback(self.roles, ctx, self.owner, role=self.newbie)
        self.assertIn("server owner", ctx.fails[0])
        self.assertNotIn(self.newbie, self.owner.roles)
        ctx = self.context()
        await self.roles.role_add.callback(self.roles, ctx, self.member, role=self.guild.default_role)
        self.assertIn("@everyone", ctx.fails[0])
        managed = self.guild.role("Bot Role", 2, managed=True)
        ctx = self.context()
        await self.roles.role_add.callback(self.roles, ctx, self.member, role=managed)
        self.assertIn("integration", ctx.fails[0])

    async def test_a_moderator_can_change_their_own_lower_roles(self):
        ctx = self.context(author=self.mod)
        await self.roles.role_add.callback(self.roles, ctx, self.mod, role=self.newbie)
        self.assertIn(self.newbie, self.mod.roles)

    async def test_the_owner_can_act_on_anyone_and_any_role_under_the_bot(self):
        ctx = self.context(author=self.owner)
        await self.roles.role_add.callback(self.roles, ctx, self.admin, role=self.pro)
        self.assertIn(self.pro, self.admin.roles)

    async def test_custom_adds_and_removes_together(self):
        self.member.roles.append(self.newbie)
        ctx = self.context()
        await self.roles.role_custom.callback(self.roles, ctx, self.member, roles="+Pro -Newbie")
        self.assertIn(self.pro, self.member.roles)
        self.assertNotIn(self.newbie, self.member.roles)
        ctx = self.context()
        await self.roles.role_custom.callback(self.roles, ctx, self.member, roles="+Nothing")
        self.assertIn("Couldn't find", ctx.fails[0])
        ctx = self.context()
        await self.roles.role_custom.callback(self.roles, ctx, self.member, roles="   ")
        self.assertEqual(len(ctx.fails), 1)

    async def test_custom_stops_before_changing_anything_when_one_role_is_out_of_reach(self):
        high = self.guild.role("High", 20)
        ctx = self.context()
        await self.roles.role_custom.callback(self.roles, ctx, self.member, roles=f"+Pro +{high.mention}")
        self.assertNotIn(self.pro, self.member.roles)
        self.assertEqual(len(ctx.fails), 1)

    async def test_the_bare_text_form_flips_a_role(self):
        ctx = self.context()
        await self.roles.role_group.callback(self.roles, ctx, self.member, role="pro")
        self.assertIn(self.pro, self.member.roles)
        await self.roles.role_group.callback(self.roles, ctx, self.member, role="pro")
        self.assertNotIn(self.pro, self.member.roles)
        await self.roles.role_group.callback(self.roles, ctx, self.member, role="nothing")
        self.assertIn("no role called", ctx.fails[0])
        await self.roles.role_group.callback(self.roles, ctx)
        self.assertEqual(len(ctx.sent), 1)

    async def test_removeall_leaves_what_the_author_cant_touch(self):
        high = self.guild.role("Top", 20)
        self.member.roles.extend([self.pro, self.newbie, high])
        ctx = self.context(author=self.owner)
        await self.roles.role_removeall.callback(self.roles, ctx, self.member)
        self.assertEqual(sorted(r.name for r in self.member.roles), sorted(["@everyone", "Top"]))
        ctx = self.context()
        await self.roles.role_removeall.callback(self.roles, ctx, self.owner)
        self.assertEqual(len(ctx.fails), 1)

    async def test_create(self):
        ctx = self.context()
        await self.roles.role_create.callback(self.roles, ctx, "Fresh", "FF0000", True, True)
        made = next(r for r in self.guild.roles if r.name == "Fresh")
        self.assertEqual(made.color.value, 0xFF0000)
        self.assertTrue(made.hoist)
        self.assertTrue(made.mentionable)
        ctx = self.context()
        await self.roles.role_create.callback(self.roles, ctx, "x" * 101)
        self.assertEqual(len(ctx.fails), 1)
        with self.assertRaises(Invalid):
            await self.roles.role_create.callback(self.roles, self.context(), "Bad", "nope")

    async def test_color(self):
        ctx = self.context()
        await self.roles.role_color.callback(self.roles, ctx, self.pro, "#00ff00")
        self.assertEqual(self.pro.color.value, 0x00FF00)
        high = self.guild.role("High", 20)
        ctx = self.context()
        await self.roles.role_color.callback(self.roles, ctx, high, "#00ff00")
        self.assertEqual(len(ctx.fails), 1)

    async def test_info_and_lists_never_ping(self):
        ctx = self.context(author=self.member)
        await self.roles.role_info.callback(self.roles, ctx, role=self.role_admin)
        embed = ctx.sent[0]["embed"]
        self.assertEqual(embed.title, "Admin")
        self.assertIn("Administrator", embed.fields[-1].value)
        self.assertFalse(ctx.sent[0]["allowed_mentions"].roles)
        ctx = self.context(author=self.member)
        await self.roles.role_allroles.callback(self.roles, ctx)
        self.assertIn(self.role_admin.mention, ctx.sent[0]["embed"].description)
        self.assertNotIn(self.guild.default_role.mention, ctx.sent[0]["embed"].description)
        self.assertFalse(ctx.sent[0]["allowed_mentions"].roles)

    async def test_a_long_role_list_splits_into_pages(self):
        for index in range(150):
            self.guild.role(f"A role with a fairly long name number {index}", 1)
        ctx = self.context()
        await self.roles.allroles.callback(self.roles, ctx)
        self.assertGreater(len(ctx.sent), 1)
        for sent in ctx.sent:
            self.assertLessEqual(len(sent["embed"].description), 4096)

    async def test_diagnose(self):
        high = self.guild.role("High", 20)
        strong = self.guild.role("Strong", 3, "ban_members")
        await self.bot.settings.save(self.guild.id, "roles", {"ranks": [self.pro.id]})
        for role, expected in ((high, "at or above the bot"), (strong, "Ban Members"), (self.pro, "rank list"), (self.guild.default_role, "@everyone")):
            ctx = self.context(author=self.member)
            await self.roles.role_diagnose.callback(self.roles, ctx, role=role)
            self.assertIn(expected, ctx.sent[0]["embed"].description)
        ctx = self.context()
        await self.roles.rolediagnose.callback(self.roles, ctx)
        self.assertIn("Manage Roles", ctx.sent[0]["embed"].description)


class ReactionRoleDiagnoseTest(Base):
    modules = [Roles, ReactionRoles]

    async def test_diagnose_counts_reaction_role_messages(self):
        rr = self.cog(ReactionRoles)
        await self.db.run("INSERT INTO rr_messages (message_id, guild_id, channel_id, created_at) VALUES (111111111111111111, ?, ?, 1)", (self.guild.id, self.channel.id))
        await self.db.run("INSERT INTO rr_pairs (guild_id, message_id, slot, emoji, emoji_key, role_id) VALUES (?, 111111111111111111, 0, 'x', 'x', ?)", (self.guild.id, self.pro.id))
        self.assertEqual(await rr.messages_using_role(self.guild.id, self.pro.id), [111111111111111111])
        ctx = self.context()
        await self.roles.role_diagnose.callback(self.roles, ctx, role=self.pro)
        self.assertIn("1 reaction role messages", ctx.sent[0]["embed"].description)


class BulkTest(Base):
    async def run_job(self) -> None:
        job = self.roles.jobs.get(self.guild.id)
        if job and job.task:
            await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), 10)

    def post(self) -> object:
        return self.channel.sent[-1]

    async def test_all_gives_a_role_to_every_member_and_reports(self):
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        self.assertIn("Adding Pro", self.post().embeds[0].title)
        await self.run_job()
        for member in self.guild.members:
            self.assertIn(self.pro, member.roles)
        self.assertIn("Finished", self.post().embeds[0].description)
        self.assertIsNone(self.post().kwargs.get("view"))
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_humans_and_bots(self):
        ctx = self.context()
        await self.roles.role_humans.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertNotIn(self.pro, self.me.roles)
        self.assertIn(self.pro, self.member.roles)
        await self.roles.role_bots.callback(self.roles, ctx, role=self.newbie)
        await self.run_job()
        self.assertIn(self.newbie, self.me.roles)
        self.assertNotIn(self.newbie, self.member.roles)

    async def test_in_and_removein(self):
        ctx = self.context()
        await self.roles.role_in.callback(self.roles, ctx, self.role_mod, new_role=self.pro)
        await self.run_job()
        self.assertIn(self.pro, self.mod.roles)
        self.assertNotIn(self.pro, self.member.roles)
        await self.roles.role_removein.callback(self.roles, ctx, self.role_mod, new_role=self.pro)
        await self.run_job()
        self.assertNotIn(self.pro, self.mod.roles)

    async def test_rall_removebots_removehumans(self):
        self.member.roles.append(self.pro)
        self.me.roles.append(self.pro)
        ctx = self.context()
        await self.roles.role_removebots.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertNotIn(self.pro, self.me.roles)
        self.assertIn(self.pro, self.member.roles)
        await self.roles.role_removehumans.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertNotIn(self.pro, self.member.roles)
        self.member.roles.append(self.newbie)
        await self.roles.role_rall.callback(self.roles, ctx, role=self.newbie)
        await self.run_job()
        self.assertNotIn(self.newbie, self.member.roles)

    async def test_someone_on_the_rules_screen_is_skipped(self):
        waiting = FakeMember(self.guild, "waiting", pending=True)
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertNotIn(self.pro, waiting.roles)

    async def test_nobody_needing_it_says_so(self):
        for member in self.guild.members:
            member.roles.append(self.pro)
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        self.assertIn("Nobody", ctx.oks[0])
        self.assertEqual(self.roles.jobs, {})

    async def test_refusals(self):
        high = self.guild.role("High", 20)
        strong = self.guild.role("Strong", 3, "ban_members")
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=high)
        self.assertIn("bot's highest role", ctx.fails[0])
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=strong)
        self.assertIn("elevated roles", ctx.fails[0])
        ctx = self.context(author=self.mod)
        await self.roles.role_all.callback(self.roles, ctx, role=self.guild.role("Above Mod", 7))
        self.assertIn("your highest role", ctx.fails[0])
        self.assertEqual(self.roles.jobs, {})

    async def test_only_one_at_a_time(self):
        gate = asyncio.Event()
        real = self.member.add_roles

        async def held(*roles, **kwargs):
            await gate.wait()
            await real(*roles, **kwargs)

        self.member.add_roles = held
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await asyncio.sleep(0)
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.newbie)
        self.assertIn("already running", ctx.fails[0])
        gate.set()
        await self.run_job()

    async def test_cancel_by_command(self):
        with mock.patch.object(module, "BULK_SPACING", 0.01):
            for index in range(30):
                FakeMember(self.guild, f"m{index}")
            ctx = self.context()
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            await asyncio.sleep(0.03)
            await self.roles.role_bulk_cancel.callback(self.roles, self.context())
            await self.run_job()
        self.assertIn("Canceled by admin", self.post().embeds[0].description)
        have = [m for m in self.guild.members if self.pro in m.roles]
        self.assertLess(len(have), len(self.guild.members))
        self.assertEqual(self.roles.jobs, {})
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))
        ctx = self.context()
        await self.roles.role_bulk_cancel.callback(self.roles, ctx)
        self.assertIn("No bulk", ctx.fails[0])

    async def interaction_for(self, member):
        sent = []

        class Response:
            async def send_message(self, content=None, **kwargs):
                sent.append((content, kwargs))

            async def edit_message(self, **kwargs):
                sent.append(("edit", kwargs))

        return SimpleNamespace(guild_id=self.guild.id, user=SimpleNamespace(id=member.id, display_name=member.name, guild_permissions=member.guild_permissions), message=None, response=Response()), sent

    async def test_cancel_button_checks_who_clicked(self):
        with mock.patch.object(module, "BULK_SPACING", 0.01):
            for index in range(30):
                FakeMember(self.guild, f"m{index}")
            ctx = self.context()
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            event, sent = await self.interaction_for(self.member)
            await self.roles.cancel_clicked(event)
            self.assertIn("Manage Roles", sent[0][0])
            self.assertIsNone(self.roles.jobs[self.guild.id].canceled_by)
            event, sent = await self.interaction_for(self.admin)
            await self.roles.cancel_clicked(event)
            self.assertIn("Canceling", sent[0][0])
            await self.run_job()
        event, sent = await self.interaction_for(self.admin)
        await self.roles.cancel_clicked(event)
        self.assertEqual(sent[0][0], "edit")

    async def test_a_restart_picks_up_where_it_stopped(self):
        with mock.patch.object(module, "BULK_SPACING", 0.01):
            for index in range(30):
                FakeMember(self.guild, f"m{index}")
            ctx = self.context()
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            await asyncio.sleep(0.05)
            await self.roles.cog_unload()
            await asyncio.sleep(0)
        partway = [m for m in self.guild.members if self.pro in m.roles]
        self.assertLess(len(partway), len(self.guild.members))
        self.assertIsNotNone(await self.db.one("SELECT * FROM role_bulk_jobs"))
        self.assertEqual(self.roles.jobs, {})
        # Nothing about the job lives outside the database, as after a real restart.
        fresh = Roles(self.bot)
        await fresh.resume_jobs()
        job = fresh.jobs[self.guild.id]
        await asyncio.wait_for(job.task, 10)
        for member in self.guild.members:
            self.assertIn(self.pro, member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))
        self.assertIn("Finished", self.post().embeds[0].description)

    async def test_a_restored_backup_doesnt_restart_an_old_job(self):
        await self.db.run("INSERT INTO role_bulk_jobs (guild_id, action, scope, role_id, user_id, total, started_at) VALUES (?, 'add', 'all', ?, ?, 5, 1)", (self.guild.id, self.pro.id, self.admin.id))
        await self.roles.on_guild_restored(self.guild.id)
        await self.roles.resume_jobs()
        self.assertEqual(self.roles.jobs, {})
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_a_restored_backup_leaves_a_running_job_alone(self):
        with mock.patch.object(module, "BULK_SPACING", 0.01):
            for index in range(20):
                FakeMember(self.guild, f"m{index}")
            ctx = self.context()
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            await self.roles.on_guild_restored(self.guild.id)
            await self.run_job()
        for member in self.guild.members:
            self.assertIn(self.pro, member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_a_job_started_after_a_restore_still_resumes(self):
        await self.roles.on_guild_restored(self.guild.id)
        await asyncio.sleep(0.01)
        await self.db.run("INSERT INTO role_bulk_jobs (guild_id, action, scope, role_id, user_id, total, started_at) VALUES (?, 'add', 'all', ?, ?, 5, ?)", (self.guild.id, self.pro.id, self.admin.id, time.time()))
        await self.roles.resume_jobs()
        await self.run_job()
        self.assertIn(self.pro, self.member.roles)

    async def test_a_member_who_left_is_skipped(self):
        leaver = FakeMember(self.guild, "leaver")

        async def gone(*roles, **kwargs):
            raise discord.NotFound(_FakeResponse(404), "Unknown Member")

        leaver.add_roles = gone
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertIn("Finished", self.post().embeds[0].description)
        self.assertIn(self.pro, self.member.roles)

    async def test_a_deleted_role_stops_it(self):
        with mock.patch.object(module, "BULK_SPACING", 0.01):
            for index in range(20):
                FakeMember(self.guild, f"m{index}")
            ctx = self.context()
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            await asyncio.sleep(0.03)
            self.guild.roles.remove(self.pro)
            await self.run_job()
        self.assertIn("no longer exists", self.post().embeds[0].description)

    async def test_failures_in_a_row_stop_it_and_say_so(self):
        for index in range(module.BULK_FAILURES_TO_STOP + 2):
            person = FakeMember(self.guild, f"m{index}")

            async def refused(*roles, **kwargs):
                raise discord.Forbidden(_FakeResponse(403), "no")

            person.add_roles = refused
        self.guild.members.sort(key=lambda m: 0 if m.add_roles.__qualname__.endswith("refused") else 1)
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertIn("failures in a row", self.post().embeds[0].description)
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_one_failure_doesnt_stop_it(self):
        first = self.guild.members[0]
        real = first.add_roles
        tries = []

        async def once(*roles, **kwargs):
            if not tries:
                tries.append(1)
                raise discord.Forbidden(_FakeResponse(403), "no")
            await real(*roles, **kwargs)

        first.add_roles = once
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertIn(self.pro, self.member.roles)
        self.assertIn("1 failed", self.post().embeds[0].description)

    async def test_an_outage_is_waited_out_on_the_same_member(self):
        first = self.guild.members[0]
        real = first.add_roles
        problems = [discord.HTTPException(_FakeResponse(503), "down"), aiohttp.ClientOSError(104, "connection reset"), asyncio.TimeoutError(), discord.HTTPException(_FakeResponse(429), "cloudflare")]

        async def down(*roles, **kwargs):
            if problems:
                raise problems.pop()
            await real(*roles, **kwargs)

        first.add_roles = down
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertEqual(problems, [])
        self.assertIn(self.pro, first.roles)
        self.assertIn(self.pro, self.member.roles)
        self.assertIn("Finished", self.post().embeds[0].description)
        self.assertNotIn("failed", self.post().embeds[0].description)

    async def test_canceling_stops_a_job_waiting_out_an_outage(self):
        first = self.guild.members[0]

        async def down(*roles, **kwargs):
            raise discord.HTTPException(_FakeResponse(503), "down")

        first.add_roles = down
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await asyncio.sleep(0.01)
        self.roles.jobs[self.guild.id].canceled_by = "mod"
        await self.run_job()
        self.assertIn("Canceled by mod", self.post().embeds[0].description)

    async def test_an_unexpected_error_keeps_the_row_for_the_next_start(self):
        ctx = self.context()
        with mock.patch.object(self.roles, "_targets", side_effect=[list(range(3)), RuntimeError("a bug")]):
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            await self.run_job()
        self.assertIn("picks up at the next start", self.post().embeds[0].description)
        self.assertIsNotNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_a_new_job_can_start_after_one_stopped_on_an_error(self):
        ctx = self.context()
        with mock.patch.object(self.roles, "_targets", side_effect=[list(range(3)), RuntimeError("a bug")]):
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            await self.run_job()
        await self.roles.role_all.callback(self.roles, self.context(), role=self.newbie)
        await self.run_job()
        self.assertIn(self.newbie, self.member.roles)
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_a_progress_post_that_fails_doesnt_end_the_job(self):
        job = BulkJob(self.guild.id, "add", "all", self.pro.id, None, self.admin.id, self.channel.id, 123)
        partial = SimpleNamespace(edit=mock.AsyncMock(side_effect=aiohttp.ClientOSError(104, "connection reset")))
        with mock.patch.object(self.channel, "get_partial_message", return_value=partial):
            await self.roles._update_post(self.guild, job)

    async def insert_job(self, *, started_at=None, saved_at=None) -> None:
        await self.db.run(
            "INSERT INTO role_bulk_jobs (guild_id, action, scope, role_id, user_id, channel_id, message_id, total, started_at, saved_at) VALUES (?, 'add', 'all', ?, ?, ?, ?, 5, ?, ?)",
            (self.guild.id, self.pro.id, self.admin.id, self.channel.id, 123, time.time() if started_at is None else started_at, saved_at),
        )

    async def test_a_job_stopped_for_hours_waits_to_be_asked_for_again(self):
        """Put back from a copy of the database, a job nobody wants now would otherwise pick up and change roles for everyone."""
        then = time.time() - module.BULK_RESUME_LIMIT - 60
        await self.insert_job(started_at=then, saved_at=then)
        with mock.patch.object(self.roles, "_update_post", new_callable=mock.AsyncMock) as post:
            await self.roles.resume_jobs()
        self.assertEqual(self.roles.jobs, {})
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))
        self.assertNotIn(self.pro, self.member.roles)
        self.assertIn("Start it again", post.await_args.kwargs["ending"])

    async def test_a_long_job_that_saved_recently_picks_up(self):
        await self.insert_job(started_at=time.time() - module.BULK_RESUME_LIMIT * 2, saved_at=time.time() - 60)
        await self.roles.resume_jobs()
        await self.run_job()
        self.assertIn(self.pro, self.member.roles)

    async def test_progress_saves_say_when(self):
        with mock.patch.object(module, "BULK_SAVE_EVERY", 1):
            ctx = self.context()
            await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
            for _ in range(200):
                row = await self.db.one("SELECT saved_at FROM role_bulk_jobs")
                if row is None or row["saved_at"] is not None:
                    break
                await asyncio.sleep(0.001)
            await self.run_job()

    async def test_a_muted_command_runs_with_no_post(self):
        ctx = self.context()
        ctx.decision = SimpleNamespace(muted=True, redirect=None, restricted_channel=None)
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertIn(self.pro, self.member.roles)
        self.assertEqual(self.channel.sent, [])

    async def test_a_redirect_moves_the_post(self):
        other = FakeChannel(self.guild, "bot-output")
        ctx = self.context()
        ctx.decision = SimpleNamespace(muted=False, redirect=other.id, restricted_channel=None)
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        await self.run_job()
        self.assertEqual(len(other.sent), 1)
        self.assertEqual(self.channel.sent, [])

    async def test_a_deleted_post_doesnt_stop_it(self):
        ctx = self.context()
        await self.roles.role_all.callback(self.roles, ctx, role=self.pro)
        self.post().deleted = True
        self.channel.sent.clear()
        await self.run_job()
        self.assertIn(self.pro, self.member.roles)

    def test_time_left_comes_from_how_fast_it_has_gone(self):
        job = BulkJob(1, "add", "all", 2, None, 3, None, None, done=10, total=110, run_started_at=time.time() - 100, run_done=10)
        self.assertAlmostEqual(job.seconds_left(), 1000, delta=20)
        job = BulkJob(1, "add", "all", 2, None, 3, None, None, done=1, total=101, run_started_at=time.time(), run_done=1)
        with mock.patch.object(module, "BULK_SPACING", 2):
            self.assertEqual(job.seconds_left(), 200)


class TempRoleTest(Base):
    async def test_it_ends_for_someone_the_cache_doesnt_have(self):
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        self.guild.members.remove(self.member)
        self.guild.fetch_member = mock.AsyncMock(return_value=self.member)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 1)
        self.assertNotIn(self.pro, self.member.roles)

    async def test_a_lookup_that_fails_keeps_the_timer(self):
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        self.guild.members.remove(self.member)
        self.guild.fetch_member = mock.AsyncMock(side_effect=discord.HTTPException(_FakeResponse(503), "down"))
        await self.bot.timers.run_due(time.time() + 3601)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM timers WHERE kind = 'temprole'"), 1)
        self.assertIn(self.pro, self.member.roles)

    async def test_add_with_the_time_before_or_after_the_role(self):
        for text in (("2h", "Pro"), ("Pro", "2h")):
            ctx = self.context()
            await self.roles.temprole_add.callback(self.roles, ctx, self.member, text[0], role=text[1])
            self.assertIn(self.pro, self.member.roles)
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "Two", role="Words 2h")
        self.assertIn("no role called `Two Words`", ctx.fails[0])
        two = self.guild.role("Two Words", 2)
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "Two", role="Words 2h")
        self.assertIn(two, self.member.roles)

    async def test_it_ends_on_its_timer(self):
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 1)
        self.assertNotIn(self.pro, self.member.roles)

    async def test_a_restart_keeps_it(self):
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        fresh = Roles(self.bot)
        await fresh.on_load()
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 1)
        self.assertNotIn(self.pro, self.member.roles)

    async def test_bad_times_and_roles(self):
        for duration, role, expected in (("soon", "Pro", "length of time"), ("1s", "Pro", "one minute"), ("2y", "Pro", "one year"), ("1h", "Nothing", "no role called")):
            ctx = self.context()
            await self.roles.temprole_add.callback(self.roles, ctx, self.member, duration, role=role)
            self.assertIn(expected, ctx.fails[0])
        self.assertNotIn(self.pro, self.member.roles)

    async def test_the_same_hierarchy_rules_apply(self):
        ctx = self.context(author=self.mod)
        await self.roles.temprole_add.callback(self.roles, ctx, self.admin, "1h", role="Newbie")
        self.assertIn("at or above your highest role", ctx.fails[0])

    async def test_custom_gives_and_takes_for_a_while(self):
        self.member.roles.append(self.newbie)
        ctx = self.context()
        await self.roles.temprole_custom.callback(self.roles, ctx, self.member, "3h", roles="+Pro -Newbie")
        self.assertIn(self.pro, self.member.roles)
        self.assertNotIn(self.newbie, self.member.roles)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 10801), 2)
        self.assertNotIn(self.pro, self.member.roles)
        self.assertIn(self.newbie, self.member.roles)

    async def test_custom_refuses_a_bad_time_or_role(self):
        ctx = self.context()
        await self.roles.temprole_custom.callback(self.roles, ctx, self.member, "soon", roles="+Pro")
        self.assertIn("length of time", ctx.fails[0])
        ctx = self.context()
        await self.roles.temprole_custom.callback(self.roles, ctx, self.member, "3h", roles="+Nothing")
        self.assertIn("Couldn't find", ctx.fails[0])
        ctx = self.context()
        await self.roles.temprole_custom.callback(self.roles, ctx, self.member, "3h", roles=" ")
        self.assertEqual(len(ctx.fails), 1)

    async def test_the_bare_text_form(self):
        ctx = self.context()
        await self.roles.temprole.callback(self.roles, ctx, self.member, rest="2h Pro")
        self.assertIn(self.pro, self.member.roles)
        await self.roles.temprole.callback(self.roles, ctx)
        self.assertEqual(len(ctx.sent), 1)

    async def test_a_plain_role_command_ends_the_timer(self):
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        await self.roles.role_add.callback(self.roles, ctx, self.member, role=self.pro)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 0)
        self.assertIn(self.pro, self.member.roles)

    async def test_a_timer_for_someone_who_left_or_a_deleted_role_does_nothing(self):
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        await self.roles.temprole_add.callback(self.roles, ctx, self.mod, "1h", role="Newbie")
        self.guild.members.remove(self.member)
        self.guild.roles.remove(self.newbie)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 2)

    async def test_a_role_now_above_the_bot_is_left_and_logged(self):
        ctx = self.context()
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        self.pro.position = 50
        self.assertEqual(await self.bot.timers.run_due(time.time() + 3601), 1)
        self.assertIn(self.pro, self.member.roles)

    async def test_list(self):
        ctx = self.context()
        await self.roles.temprole_list.callback(self.roles, ctx)
        self.assertIn("No temporary", ctx.sent[0]["embed"].description)
        await self.roles.temprole_add.callback(self.roles, ctx, self.member, "1h", role="Pro")
        ctx = self.context()
        await self.roles.temprole_list.callback(self.roles, ctx)
        self.assertIn(self.member.mention, ctx.sent[0]["embed"].description)


class RankTest(Base):
    async def test_add_list_remove(self):
        ctx = self.context()
        await self.roles.rank_addrank.callback(self.roles, ctx, roles="Pro Newbie")
        self.assertEqual(self.settings("roles")["ranks"], [self.pro.id, self.newbie.id])
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "rank.add")
        ctx = self.context(author=self.member)
        await self.roles.rank_allranks.callback(self.roles, ctx)
        self.assertIn(self.pro.mention, ctx.sent[0]["embed"].description)
        self.assertFalse(ctx.sent[0]["allowed_mentions"].roles)
        ctx = self.context()
        await self.roles.rank_removerank.callback(self.roles, ctx, roles="Pro")
        self.assertEqual(self.settings("roles")["ranks"], [self.newbie.id])

    async def test_a_rank_must_be_something_the_author_could_hand_out(self):
        high = self.guild.role("High", 20)
        strong = self.guild.role("Strong", 3, "ban_members")
        above = self.guild.role("Above Mod", 7)
        ctx = self.context()
        await self.roles.rank_addrank.callback(self.roles, ctx, roles=high.mention)
        self.assertEqual(len(ctx.fails), 1)
        ctx = self.context()
        await self.roles.rank_addrank.callback(self.roles, ctx, roles=strong.mention)
        self.assertIn("elevated", ctx.fails[0])
        ctx = self.context(author=self.mod)
        await self.roles.rank_addrank.callback(self.roles, ctx, roles=above.mention)
        self.assertIn("your highest role", ctx.fails[0])
        ctx = self.context()
        await self.roles.rank_addrank.callback(self.roles, ctx, roles="Nothing")
        self.assertIn("Couldn't find", ctx.fails[0])
        self.assertEqual(self.settings("roles")["ranks"], [])

    async def test_a_member_flips_their_own_rank(self):
        await self.bot.settings.save(self.guild.id, "roles", {"ranks": [self.pro.id, self.newbie.id]})
        ctx = self.context(author=self.member)
        await self.roles.rank.callback(self.roles, ctx, role="pro")
        self.assertIn(self.pro, self.member.roles)
        await self.roles.rank.callback(self.roles, ctx, role="pro")
        self.assertNotIn(self.pro, self.member.roles)
        ctx = self.context(author=self.member)
        await self.roles.rank_custom.callback(self.roles, ctx, roles="+Pro -Newbie")
        self.assertIn(self.pro, self.member.roles)
        self.assertIn("You now have", ctx.oks[0])
        ctx = self.context(author=self.member)
        await self.roles.rank.callback(self.roles, ctx)
        self.assertEqual(len(ctx.sent), 1)

    async def test_only_the_list_counts(self):
        await self.bot.settings.save(self.guild.id, "roles", {"ranks": [self.pro.id]})
        ctx = self.context(author=self.member)
        await self.roles.rank_custom.callback(self.roles, ctx, roles="+Pro +Newbie")
        self.assertIn("isn't on the rank list", ctx.fails[0])
        self.assertNotIn(self.pro, self.member.roles)

    async def test_a_rank_that_got_dangerous_is_refused(self):
        strong = self.guild.role("Later Strong", 3)
        await self.bot.settings.save(self.guild.id, "roles", {"ranks": [strong.id]})
        strong.permissions = type(strong.permissions)("ban_members")
        ctx = self.context(author=self.member)
        await self.roles.rank_custom.callback(self.roles, ctx, roles="+Later Strong")
        self.assertIn("elevated", ctx.fails[0])
        self.assertNotIn(strong, self.member.roles)

    async def test_a_rank_now_above_the_bot_is_refused(self):
        await self.bot.settings.save(self.guild.id, "roles", {"ranks": [self.pro.id]})
        self.pro.position = 50
        ctx = self.context(author=self.member)
        await self.roles.rank_custom.callback(self.roles, ctx, roles="Pro")
        self.assertIn("bot's highest role", ctx.fails[0])

    async def test_the_short_forms(self):
        ctx = self.context()
        await self.roles.addrank.callback(self.roles, ctx, roles="Pro")
        self.assertEqual(self.settings("roles")["ranks"], [self.pro.id])
        await self.roles.ranks.callback(self.roles, ctx)
        self.assertIn(self.pro.mention, ctx.sent[-1]["embed"].description)
        await self.roles.removerank.callback(self.roles, ctx, roles="Pro")
        self.assertEqual(self.settings("roles")["ranks"], [])

    async def test_ranks_save_from_the_dashboard(self):
        client = await self.client()
        response = await client.put(self.path("/settings/roles"), json={"ranks": [str(self.pro.id)]})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("roles")["ranks"], [self.pro.id])
        self.assertEqual((await client.put(self.path("/settings/roles"), json={"ranks": ["nope"]})).status, 400)
        staff = await self.client(access="staff")
        self.assertEqual((await staff.put(self.path("/settings/roles"), json={"ranks": []})).status, 403)


class RealLibraryTest(GatewayTest):
    modules = [Roles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.spacing = mock.patch.object(module, "BULK_SPACING", 0)
        self.spacing.start()
        self.roles = next(m for m in self.bot.modules if isinstance(m, Roles))

    async def asyncTearDown(self) -> None:
        self.spacing.stop()
        await super().asyncTearDown()

    def slash(self, root: str, command: str, options: list[dict], *, user_id: int | None = None, resolved: dict | None = None) -> None:
        data = {"id": str(gateway_id()), "name": root, "type": 1, "options": [{"name": command, "type": 1, "options": options}], "resolved": resolved or {}}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.owner_id))

    def role_option(self, name: str, role: dict) -> tuple[dict, dict]:
        return {"name": name, "type": 8, "value": role["id"]}, {"roles": {role["id"]: role}}

    def member_option(self, name: str, user_id: int) -> tuple[dict, dict]:
        member = self.member_json[user_id]
        return {"name": name, "type": 6, "value": str(user_id)}, {"users": {str(user_id): member["user"]}, "members": {str(user_id): {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}}}

    async def test_role_add_by_slash_reaches_discord(self):
        who, resolved = self.member_option("user", self.member_id)
        role, more = self.role_option("role", self.role_mod)
        self.slash("role", "add", [who, role], resolved={**resolved, **more})
        await self.settle()
        self.assert_no_errors()
        self.assertTrue(any(m == "PUT" and p == "/guilds/{guild_id}/members/{user_id}/roles/{role_id}" for m, p, _ in self.net.calls))

    async def test_a_bulk_job_runs_and_finishes_through_the_library(self):
        role, resolved = self.role_option("role", self.role_muted)
        self.slash("role", "humans", [role], resolved=resolved)
        await self.settle()
        job = next(iter(self.roles.jobs.values()), None)
        if job and job.task:
            await asyncio.wait_for(job.task, 10)
        for _ in range(500):  # a job leaves the list just before its last edit of the post, so a busy machine can get here between the two
            posts = [message for message in self.net.posted if message.get("embeds")]
            if posts and "Finished" in posts[-1]["embeds"][0]["description"]:
                break
            await asyncio.sleep(0.02)
        self.assert_no_errors()
        puts = [1 for m, p, _ in self.net.calls if m == "PUT" and p.endswith("/roles/{role_id}")]
        self.assertEqual(len(puts), 4)  # everyone but the bot
        posts = [message for message in self.net.posted if message.get("embeds")]
        self.assertIn("Finished", posts[-1]["embeds"][0]["description"])
        self.assertEqual(posts[-1]["components"], [])

    async def test_the_cancel_button_answers_after_a_restart(self):
        fresh_view_ids = [item.custom_id for view in self.bot.persistent_views for item in view.children]
        self.assertIn("role:bulk:cancel", fresh_view_ids)
        message = self.net.message_json(int(self.text["id"]), {"content": "", "embeds": [], "components": [{"type": 1, "components": [{"type": 2, "custom_id": "role:bulk:cancel", "style": 4, "label": "Cancel"}]}]})
        data = {"custom_id": "role:bulk:cancel", "component_type": 2}
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=self.owner_id, message=message))
        await self.settle()
        self.assert_no_errors()
        callback = next(body for method, path, body in self.net.calls if path.endswith("/callback"))
        self.assertEqual(callback["type"], 7)

    async def test_every_role_command_succeeds_through_the_library(self):
        """Valid input on every slash command, which the smoke test's made-up values mostly don't reach."""
        who, who_resolved = self.member_option("user", self.member_id)
        member, member_resolved = self.member_option("member", self.member_id)
        role, role_resolved = self.role_option("role", self.role_muted)
        resolved = {"users": {**who_resolved["users"]}, "members": {**who_resolved["members"]}, "roles": {**role_resolved["roles"]}}
        steps = [
            ("role", "allroles", []),
            ("role", "info", [role]),
            ("role", "diagnose", []),
            ("role", "diagnose", [role]),
            ("role", "create", [{"name": "name", "type": 3, "value": "Fresh"}, {"name": "color", "type": 3, "value": "FF0000"}, {"name": "mentionable", "type": 5, "value": True}, {"name": "hoist", "type": 5, "value": True}]),
            ("role", "color", [role, {"name": "color", "type": 3, "value": "00ff00"}]),
            ("role", "add", [who, role]),
            ("role", "remove", [who, role]),
            ("role", "custom", [member, {"name": "roles", "type": 3, "value": "+Muted"}]),
            ("role", "removeall", [who]),
            ("temprole", "add", [member, {"name": "duration", "type": 3, "value": "1h"}, {"name": "role", "type": 3, "value": self.role_muted["id"]}]),
            ("temprole", "custom", [member, {"name": "duration", "type": 3, "value": "1h"}, {"name": "roles", "type": 3, "value": "+Muted"}]),
            ("temprole", "list", []),
            ("rank", "addrank", [{"name": "roles", "type": 3, "value": "Muted"}]),
            ("rank", "allranks", []),
            ("rank", "custom", [{"name": "roles", "type": 3, "value": "+Muted"}]),
            ("rank", "removerank", [{"name": "roles", "type": 3, "value": "Muted"}]),
        ]
        for root, command, options in steps:
            self.net.calls.clear()
            self.slash(root, command, options, resolved=resolved)
            await self.settle()
            self.assert_no_errors(f"{root} {command}")
            replies = [body for method, path, body in self.net.calls if "/callback" in path or path.startswith("/webhooks/")]
            self.assertTrue(replies, command)
            failed = [body for body in replies if "\N{CROSS MARK}" in str((body.get("data") or body).get("content"))]
            self.assertEqual(failed, [], f"{root} {command}")
        await self.bot.timers.run_due(time.time() + 3601)
        self.assert_no_errors("timers")

    async def test_every_bulk_command_through_the_library(self):
        role, resolved = self.role_option("role", self.role_muted)
        base, base_resolved = self.role_option("base_role", self.role_member)
        new, new_resolved = self.role_option("new_role", self.role_muted)
        both = {"roles": {**base_resolved["roles"], **new_resolved["roles"]}}
        for command, options, seen in (("all", [role], resolved), ("humans", [role], resolved), ("bots", [role], resolved), ("in", [base, new], both), ("rall", [role], resolved), ("removehumans", [role], resolved), ("removebots", [role], resolved), ("removein", [base, new], both)):
            self.slash("role", command, options, resolved=seen)
            await self.settle()
            job = next(iter(self.roles.jobs.values()), None)
            if job and job.task:
                await asyncio.wait_for(job.task, 10)
            self.assert_no_errors(command)
        self.slash("role", "bulk-cancel", [])
        await self.settle()
        self.assert_no_errors("bulk-cancel")

    async def test_text_commands_parse(self):
        for text in ("!role @nobody x", "!role", "!allroles", "!ranks", "!rolediagnose", "!rank", "!temprole", f"!role add <@{self.member_id}> muted", f"!role info moderator", f"!temprole <@{self.member_id}> 2h muted", f"!temprole add <@{self.member_id}> muted 2h", f"!rank custom +muted"):
            self.dispatch_message(user_id=self.owner_id, content=text)
            await self.settle()
            self.assert_no_errors(text)
        roles = [p for m, p, _ in self.net.calls if m == "PUT" and p.endswith("/roles/{role_id}")]
        self.assertGreaterEqual(len(roles), 3)
        self.assertIsNotNone(await self.bot.timers.get_key("temprole", f"{self.guild_id}:{self.member_id}:{self.role_muted['id']}"))

    async def test_a_role_name_in_another_case_works_in_a_text_command(self):
        self.dispatch_message(user_id=self.owner_id, content=f"!role info MODERATOR")
        await self.settle()
        self.assert_no_errors()
        post = self.net.posted[-1]
        self.assertEqual(post["embeds"][0]["title"], "Moderator")

    async def test_role_options_stay_role_pickers_on_slash(self):
        role = self.bot.tree.get_command("role")
        data = next(c for c in role.commands if c.name == "add").to_dict(self.bot.tree)
        kinds = {o["name"]: o["type"] for o in data["options"]}
        self.assertEqual(kinds, {"user": 6, "role": 8})


class ShortMemberListTest(GatewayTest):
    """A restart with a job part done, when the gateway's ready event didn't wait for the whole member list."""

    modules = [Roles]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.patches = [mock.patch.object(module, "BULK_SPACING", 0), mock.patch.object(module, "BULK_WAIT", 0.01)]
        for patch in self.patches:
            patch.start()
        self.roles = next(m for m in self.bot.modules if isinstance(m, Roles))
        self.role_id = int(self.role_muted["id"])

    async def asyncTearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        await super().asyncTearDown()

    async def insert_job(self) -> None:
        await self.db.run(
            "INSERT INTO role_bulk_jobs (guild_id, action, scope, role_id, user_id, done, total, started_at) VALUES (?, 'add', 'all', ?, ?, 100, 43000, ?)",
            (self.guild_id, self.role_id, self.owner_id, time.time() - 3600),
        )

    def role_puts(self) -> list[str]:
        return [path for method, path, _ in self.net.calls if method == "PUT" and "/roles/" in path]

    async def wait_for_job(self) -> None:
        job = self.roles.jobs.get(self.guild_id)
        if job and job.task:
            await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), 10)

    async def test_a_job_waits_for_the_rest_of_the_members(self):
        await self.insert_job()
        guild = self.bot.get_guild(self.guild_id)
        missing = {user_id: guild._members.pop(user_id) for user_id in (self.mod_id, self.member_id)}

        async def chunk(_guild, *, cache=True):
            guild._members.update(missing)
            return list(guild._members.values())

        with mock.patch.object(discord.Guild, "chunk", new=chunk):
            await self.roles.on_ready()
            await self.wait_for_job()
        self.assertEqual(len(self.role_puts()), len(self.member_json))
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_a_list_that_doesnt_arrive_keeps_the_job_waiting(self):
        await self.insert_job()
        guild = self.bot.get_guild(self.guild_id)
        for user_id in (self.mod_id, self.member_id):
            guild._members.pop(user_id)
        with mock.patch.object(discord.Guild, "chunk", new=mock.AsyncMock(side_effect=asyncio.TimeoutError())):
            await self.roles.on_ready()
            await asyncio.sleep(0.05)
            self.assertEqual(self.role_puts(), [])
            self.assertIn(self.guild_id, self.roles.jobs)
            self.assertIsNotNone(await self.db.one("SELECT * FROM role_bulk_jobs"))
            self.roles.jobs[self.guild_id].canceled_by = "mod"
            await self.wait_for_job()
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))

    async def test_an_outage_at_ready_keeps_the_job_and_it_carries_on_after(self):
        await self.insert_job()
        real_guild = self.bot.get_guild(self.guild_id)
        state = self.bot._connection
        state.guild_ready_timeout = 0.01
        state.parse_ready({"v": 10, "user": self.bot_user, "guilds": [{"id": str(self.guild_id), "unavailable": True}], "session_id": "x", "resume_gateway_url": "wss://x", "application": {"id": str(self.bot_id), "flags": 0}})
        await asyncio.sleep(0.1)
        self.assertEqual(self.role_puts(), [])
        self.assertIsNotNone(await self.db.one("SELECT * FROM role_bulk_jobs"))
        self.assertIn(self.guild_id, self.roles.jobs)
        members = list(self.member_json.values())
        state.parse_guild_create({
            "id": str(self.guild_id), "name": "Test Guild", "owner_id": str(self.owner_id), "unavailable": False, "large": False, "member_count": len(members), "joined_at": "2025-01-01T00:00:00+00:00",
            "roles": list(self.net.roles.values()), "members": members, "channels": list(self.net.channels.values()), "threads": [], "emojis": [], "stickers": [], "presences": [], "voice_states": [],
            "stage_instances": [], "guild_scheduled_events": [], "soundboard_sounds": [], "features": [], "afk_timeout": 300, "verification_level": 0, "default_message_notifications": 0,
            "explicit_content_filter": 0, "mfa_level": 0, "premium_tier": 0, "preferred_locale": "en-US", "nsfw_level": 0, "system_channel_flags": 0,
        })
        await self.wait_for_job()
        self.assertEqual(len(self.role_puts()), len(members))
        self.assertIsNone(await self.db.one("SELECT * FROM role_bulk_jobs"))
        self.assertIsNot(real_guild, self.bot.get_guild(self.guild_id))

    async def test_a_new_job_refuses_to_start_on_part_of_the_server(self):
        guild = self.bot.get_guild(self.guild_id)
        for user_id in (self.mod_id, self.member_id):
            guild._members.pop(user_id)
        ctx = SimpleNamespace(guild=guild, author=guild.owner or guild.get_member(self.owner_id), interaction=None, defer=mock.AsyncMock(), fail=mock.AsyncMock(), ok=mock.AsyncMock(), channel=guild.get_channel(int(self.text["id"])))
        role = guild.get_role(self.role_id)
        with mock.patch.object(discord.Guild, "chunk", new=mock.AsyncMock(side_effect=asyncio.TimeoutError())):
            await self.roles._start_bulk(ctx, "add", "all", role)
        self.assertIn("isn't complete yet", ctx.fail.await_args.args[0])
        self.assertEqual(self.roles.jobs, {})
