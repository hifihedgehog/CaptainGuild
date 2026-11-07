import asyncio
import json
import time
from unittest import mock

import discord

from captainguild.core.tagscript import RegexGuard
from captainguild.modules.tags import Tags
from captainguild.modules.triggers import CHANNEL, ENDSWITH, EXACT, NORMAL, REGEX, ROLE, STARTSWITH, STRICT, ClearView, Triggers, imported_trigger
from captainguild.core.validate import Invalid
from tests.fakes import BotTest, FakeChannel, FakeMember, FakeMessage
from tests.gateway import GatewayTest


class TriggerBase(BotTest):
    modules = [Tags, Triggers]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.triggers = self.cog(Triggers)
        self.addAsyncCleanup(self.triggers.guard.close)

    async def make(self, kind=NORMAL, phrase="hello", response="Hi {user}", **changes):
        form = {"match_type": kind, "trigger": phrase, "message": {"content": response, "embeds": changes.pop("embeds", []), "buttons": []}, "channel_id": None, "role_wl": [], "role_bl": [], "channel_wl": [], "channel_bl": [], "cooldown": 0, **changes}
        return await self.triggers._insert(self.guild.id, form, self.admin.id)

    def said(self, channel=None):
        return [message.content for message in (channel or self.channel).sent]

    async def say(self, content, author=None, channel=None, **kwargs):
        channel = channel or self.channel
        message = FakeMessage(channel, author or self.member, content, **kwargs)
        await self.triggers.on_message(message)
        return message

    async def command(self, name, *args, author=None, channel=None, **kwargs):
        ctx = self.context(author=author, channel=channel)
        await getattr(self.triggers, name).callback(self.triggers, ctx, *args, **kwargs)
        return ctx


class MatchingTest(TriggerBase):
    async def fires(self, kind, phrase, content, **changes):
        await self.db.run("DELETE FROM triggers")
        self.triggers._changed(self.guild.id)
        self.channel.sent.clear()
        await self.make(kind, phrase, "yes", **changes)
        await self.say(content)
        return bool(self.said())

    async def test_normal_finds_a_substring_in_any_case(self):
        self.assertTrue(await self.fires(NORMAL, "hi", "oh HI there"))
        self.assertTrue(await self.fires(NORMAL, "hi", "this"))
        self.assertFalse(await self.fires(NORMAL, "hi", "hello"))

    async def test_strict_needs_whole_words(self):
        self.assertTrue(await self.fires(STRICT, "how do i", "So, How Do I print this?"))
        self.assertTrue(await self.fires(STRICT, "how do i", "how   do\ni"))
        self.assertFalse(await self.fires(STRICT, "hell", "hello"))
        self.assertFalse(await self.fires(STRICT, "how do i", "how do it"))
        self.assertTrue(await self.fires(STRICT, "c++", "I like C++ a lot"))

    async def test_exact_ignores_capitals_and_punctuation(self):
        self.assertTrue(await self.fires(EXACT, "hello there", "Hello, there!"))
        self.assertFalse(await self.fires(EXACT, "hello there", "hello there friend"))
        self.assertTrue(await self.fires(EXACT, "!!", "!!"))
        self.assertFalse(await self.fires(EXACT, "!!", "?"))

    async def test_startswith_and_endswith(self):
        self.assertTrue(await self.fires(STARTSWITH, "hey", "HEY you"))
        self.assertFalse(await self.fires(STARTSWITH, "hey", "you hey"))
        self.assertTrue(await self.fires(ENDSWITH, "please", "do it PLEASE"))
        self.assertFalse(await self.fires(ENDSWITH, "please", "please do it"))

    async def test_channel_specific_listens_in_one_channel(self):
        other = FakeChannel(self.guild, "other")
        await self.make(CHANNEL, "hello", "yes", channel_id=other.id)
        await self.say("hello")
        self.assertEqual(self.said(), [])
        await self.say("hello", channel=other)
        self.assertEqual(self.said(other), ["yes"])

    async def test_channel_specific_listens_through_a_channel_ignore_but_not_a_member_ignore(self):
        await self.make(CHANNEL, "hello", "yes", channel_id=self.channel.id)
        await self.make(NORMAL, "hello", "normal")
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [self.channel.id]})
        await self.say("hello")
        self.assertEqual(self.said(), ["yes"])
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [], "ignored_members": [self.member.id]})
        self.channel.sent.clear()
        await self.say("hello")
        self.assertEqual(self.said(), [])

    async def test_bots_and_webhooks_never_fire_triggers(self):
        await self.make(NORMAL, "hello", "yes")
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.say("hello", author=robot)
        hook = FakeMessage(self.channel, self.member, "hello")
        hook.webhook_id = 5
        await self.triggers.on_message(hook)
        self.assertEqual(self.said(), [])

    async def test_a_system_message_does_not_fire(self):
        await self.make(NORMAL, "hello", "yes")
        message = FakeMessage(self.channel, self.member, "hello")
        message.type = discord.MessageType.new_member
        await self.triggers.on_message(message)
        self.assertEqual(self.said(), [])

    async def test_a_command_or_tag_being_run_does_not_fire(self):
        await self.make(NORMAL, "support", "yes")
        await self.cog(Tags)._insert(self.guild.id, "support", {"content": "x", "embeds": [], "buttons": []}, owner_id=1, trusted=True)
        await self.say("!tag support")
        await self.say("!SUPPORT please")
        self.assertEqual(self.said(), [])
        await self.say("support please")
        self.assertEqual(self.said(), ["yes"])

    async def test_a_prefix_word_nobody_knows_is_chat(self):
        await self.make(NORMAL, "hello", "yes")
        await self.say("!hello")
        self.assertEqual(self.said(), ["yes"])

    async def test_ignored_channels_and_members(self):
        await self.make(NORMAL, "hello", "yes")
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [self.channel.id]})
        await self.say("hello")
        self.assertEqual(self.said(), [])
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [], "ignored_members": [self.member.id]})
        await self.say("hello")
        self.assertEqual(self.said(), [])
        await self.say("hello", author=self.mod)
        self.assertEqual(self.said(), ["yes"])

    async def test_an_ignored_category_covers_its_channels(self):
        category = FakeChannel(self.guild, "cat", discord.ChannelType.category)
        inside = FakeChannel(self.guild, "inside", category=category)
        await self.make(NORMAL, "hello", "yes")
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [category.id]})
        await self.say("hello", channel=inside)
        self.assertEqual(self.said(inside), [])

    async def test_role_lists(self):
        await self.make(NORMAL, "hello", "listed", role_wl=[self.role_mod.id])
        await self.make(NORMAL, "world", "blocked", role_bl=[self.role_mod.id])
        await self.say("hello", author=self.member)
        self.assertEqual(self.said(), [])
        await self.say("hello", author=self.mod)
        self.assertEqual(self.said(), ["listed"])
        await self.say("world", author=self.mod)
        self.assertEqual(self.said(), ["listed"])
        await self.say("world", author=self.member)
        self.assertEqual(self.said(), ["listed", "blocked"])

    async def test_channel_lists(self):
        other = FakeChannel(self.guild, "other")
        await self.make(NORMAL, "hello", "listed", channel_wl=[other.id])
        await self.make(NORMAL, "world", "blocked", channel_bl=[other.id])
        await self.say("hello")
        self.assertEqual(self.said(), [])
        await self.say("hello", channel=other)
        self.assertEqual(self.said(other), ["listed"])
        await self.say("world", channel=other)
        self.assertEqual(self.said(other), ["listed"])
        await self.say("world")
        self.assertEqual(self.said(), ["blocked"])

    async def test_cooldown_is_per_channel(self):
        other = FakeChannel(self.guild, "other")
        await self.make(NORMAL, "hello", "yes", cooldown=60)
        await self.say("hello")
        await self.say("hello")
        await self.say("hello", channel=other)
        self.assertEqual((self.said(), self.said(other)), (["yes"], ["yes"]))
        for key in list(self.triggers._fired):
            self.triggers._fired[key] -= 61
        await self.say("hello")
        self.assertEqual(self.said(), ["yes", "yes"])

    async def test_no_cooldown_means_every_time(self):
        await self.make(NORMAL, "hello", "yes")
        await self.say("hello")
        await self.say("hello")
        self.assertEqual(self.said(), ["yes", "yes"])

    async def test_one_message_sets_off_at_most_three(self):
        for phrase in ("aa", "bb", "cc", "dd", "ee"):
            await self.make(NORMAL, phrase, phrase.upper())
        await self.say("aa bb cc dd ee")
        self.assertEqual(self.said(), ["AA", "BB", "CC"])

    async def test_changes_reach_the_message_path_at_once(self):
        trigger_id = await self.make(NORMAL, "hello", "yes")
        await self.say("hello")
        await self.triggers._delete(self.guild.id, trigger_id)
        await self.say("hello")
        self.assertEqual(self.said(), ["yes"])


class ResponseTest(TriggerBase):
    async def test_args_hold_the_whole_message(self):
        await self.make(NORMAL, "hello", "{args}|{1}|{message}|{user}|{channel}")
        await self.say("well hello there")
        self.assertEqual(self.said(), ["well hello there|well|well hello there|member|general"])

    async def test_target_is_the_first_person_mentioned(self):
        await self.make(NORMAL, "hello", "{target}")
        await self.say("hello", mentions=[self.mod])
        await self.say("hello")
        self.assertEqual(self.said(), ["mod", "member"])

    async def test_mentions_can_not_ping_a_role_or_everyone(self):
        await self.make(NORMAL, "hello", "{args}")
        sent = []
        original = self.channel.send

        async def send(content=None, **kwargs):
            sent.append(kwargs)
            return await original(content, **kwargs)

        self.channel.send = send
        await self.say("hello @everyone")
        self.assertFalse(sent[0]["allowed_mentions"].everyone)
        self.assertFalse(sent[0]["allowed_mentions"].roles)

    async def test_a_response_can_be_an_embed(self):
        await self.make(NORMAL, "hello", "", embeds=[{"title": "{user}"}])
        await self.say("hello")
        self.assertEqual(self.channel.sent[0].embeds[0].title, "member")

    async def test_blocks_act_on_the_message_that_set_it_off(self):
        await self.make(NORMAL, "hello", "{delete}gone")
        await self.make(NORMAL, "world", "{reactu:x}up")
        first = await self.say("hello")
        self.assertTrue(first.deleted)
        second = await self.say("world")
        self.assertEqual(second.added_reactions, ["x"])
        self.assertEqual(self.said(), ["gone", "up"])

    async def test_a_runaway_response_stays_quiet_and_logs(self):
        await self.make(NORMAL, "hello", "{=(x):a}" + "{=(x):{x}{x}}" * 60 + "{x}")
        await self.say("hello")
        self.assertEqual(self.said(), [])

    async def test_discord_refusing_does_not_escape_the_listener(self):
        await self.make(NORMAL, "hello", "yes")
        self.channel.send = mock.AsyncMock(side_effect=discord.Forbidden(mock.Mock(status=403, reason="no"), "Missing Access"))
        await self.say("hello")
        self.channel.send.assert_awaited()

    async def test_a_failure_in_the_listener_is_logged_not_raised(self):
        with mock.patch.object(self.triggers, "rows", mock.AsyncMock(side_effect=RuntimeError("boom"))), self.assertLogs("captainguild.modules.triggers", "ERROR"):
            await self.say("hello")


class RegexTest(TriggerBase):
    async def test_a_pattern_matches_through_the_worker(self):
        await self.make(REGEX, r"hel+o\s+(world|there)", "yes")
        await self.say("say hellllo there now")
        self.assertEqual(self.said(), ["yes"])
        await self.say("say helo friend")
        self.assertEqual(self.said(), ["yes"])

    async def test_a_pattern_is_case_sensitive_unless_it_asks(self):
        await self.make(REGEX, "abc", "plain")
        await self.make(REGEX, "(?i)xyz", "any case")
        await self.say("ABC XYZ")
        self.assertEqual(self.said(), ["any case"])

    async def test_a_catastrophic_pattern_can_not_stall_the_bot(self):
        await self.make(REGEX, r"(a+)+$", "never")
        await self.make(NORMAL, "aaaa", "still here")
        ticks = 0

        async def clock():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticking = asyncio.ensure_future(clock())
        started = time.monotonic()
        await self.say("a" * 60 + "!")
        elapsed = time.monotonic() - started
        ticking.cancel()
        self.assertLess(elapsed, 5)
        self.assertGreater(ticks, 3, "the event loop kept running while the pattern ran")
        self.assertEqual(self.said(), ["still here"], "the other trigger answered")
        self.assertTrue(self.triggers.guard.took_too_long(r"(a+)+$", self.guild.id))

    async def test_slow_text_is_skipped_for_a_while_and_comes_back(self):
        guard = RegexGuard(time_limit=0.2)
        self.addAsyncCleanup(guard.close)
        pattern = r"(a+)+$"
        self.assertFalse(await guard.search(pattern, "a" * 60 + "!"))
        self.assertTrue(guard.took_too_long(pattern))
        started = time.monotonic()
        self.assertFalse(await guard.search(pattern, "a" * 60 + "!"))
        self.assertLess(time.monotonic() - started, 0.1, "text that was too slow doesn't run again")
        self.assertTrue(await guard.search("works", "this works"), "the worker comes back for other patterns")
        for key in list(guard._slow_until):
            guard._slow_until[key] = time.monotonic() - 1
        self.assertFalse(guard.took_too_long(pattern))

    async def test_a_broken_pattern_is_only_a_miss(self):
        guard = RegexGuard()
        self.addAsyncCleanup(guard.close)
        self.assertFalse(await guard.search("(", "text"))
        self.assertTrue(await guard.search("t", "text"))

    async def test_the_worker_starts_again_if_it_dies(self):
        guard = RegexGuard()
        self.addAsyncCleanup(guard.close)
        self.assertTrue(await guard.search("a", "a"))
        guard._process.kill()
        await guard._process.wait()
        self.assertTrue(await guard.search("a", "a"))

    async def test_saving_checks_a_pattern_in_the_worker(self):
        guard = self.triggers.guard
        self.assertIsNone(await guard.problem(r"hel+o"))
        self.assertIn("isn't a valid", await guard.problem("("))
        self.assertIn("at most", await guard.problem("a" * 1001))

    async def test_a_pattern_that_takes_too_long_to_compile_can_not_stall_saving(self):
        guard = RegexGuard(time_limit=0.05)
        self.addAsyncCleanup(guard.close)
        started = time.monotonic()
        self.assertIn("too long", await guard.problem("(?i)" + "[\\U00000080-\\U0010FFFF]" * 40))
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(await guard.problem("abc"), "the worker comes back")


class RoleTriggerTest(TriggerBase):
    async def test_added_and_removed(self):
        place = FakeChannel(self.guild, "changes")
        await self.make(ROLE, "", "{target} {role(name)} {role(is_added)} {role(mention)}", channel_id=place.id)
        before = mock.Mock(roles=[self.guild.default_role])
        after = FakeMember(self.guild, "someone", self.role_member)
        await self.triggers.on_member_update(before, after)
        self.assertEqual(self.said(place), [f"someone Member true <@&{self.role_member.id}>"])
        before = mock.Mock(roles=[self.guild.default_role, self.role_member])
        after.roles = [self.guild.default_role]
        await self.triggers.on_member_update(before, after)
        self.assertEqual(len(self.said(place)), 1, "the same member and role within a minute stays quiet")
        self.triggers._fired.clear()
        await self.triggers.on_member_update(before, after)
        self.assertEqual(self.said(place)[-1], f"someone Member false <@&{self.role_member.id}>")

    async def test_the_same_change_only_fires_once_a_minute(self):
        place = FakeChannel(self.guild, "changes")
        await self.make(ROLE, "", "hi", channel_id=place.id)
        before = mock.Mock(roles=[self.guild.default_role])
        after = FakeMember(self.guild, "someone", self.role_member)
        await self.triggers.on_member_update(before, after)
        await self.triggers.on_member_update(before, after)
        self.assertEqual(len(self.said(place)), 1)
        other = FakeMember(self.guild, "other", self.role_member)
        await self.triggers.on_member_update(before, other)
        self.assertEqual(len(self.said(place)), 2, "someone else's change isn't held back")

    async def test_no_change_in_roles_does_nothing(self):
        place = FakeChannel(self.guild, "changes")
        await self.make(ROLE, "", "hi", channel_id=place.id)
        member = FakeMember(self.guild, "someone", self.role_member)
        await self.triggers.on_member_update(mock.Mock(roles=list(member.roles)), member)
        self.assertEqual(self.said(place), [])

    async def test_a_bot_s_role_change_does_nothing(self):
        place = FakeChannel(self.guild, "changes")
        await self.make(ROLE, "", "hi", channel_id=place.id)
        await self.triggers.on_member_update(mock.Mock(roles=[self.guild.default_role]), FakeMember(self.guild, "robot", self.role_member, bot=True))
        self.assertEqual(self.said(place), [])

    async def test_lists_look_at_the_role_that_changed_and_the_roles_held(self):
        place = FakeChannel(self.guild, "changes")
        await self.make(ROLE, "", "listed", channel_id=place.id, role_wl=[self.role_mod.id])
        await self.make(ROLE, "", "blocked", channel_id=place.id, role_bl=[self.role_muted.id])
        before = mock.Mock(roles=[self.guild.default_role])
        await self.triggers.on_member_update(before, FakeMember(self.guild, "a", self.role_member))
        self.assertEqual(self.said(place), ["blocked"])
        await self.triggers.on_member_update(before, FakeMember(self.guild, "b", self.role_mod))
        self.assertEqual(self.said(place), ["blocked", "listed", "blocked"])
        await self.triggers.on_member_update(before, FakeMember(self.guild, "c", self.role_muted))
        self.assertEqual(self.said(place), ["blocked", "listed", "blocked"])

    async def test_user_is_whoever_changed_it(self):
        place = FakeChannel(self.guild, "changes")
        await self.make(ROLE, "", "{user} gave {target} {role}", channel_id=place.id)
        entry = mock.Mock(user=self.mod)
        with mock.patch.object(self.bot.audit, "find", mock.AsyncMock(return_value=entry)) as find:
            await self.triggers.on_member_update(mock.Mock(roles=[self.guild.default_role]), FakeMember(self.guild, "someone", self.role_member))
        self.assertEqual(find.await_args.args[1], discord.AuditLogAction.member_role_update)
        self.assertEqual(self.said(place), ["mod gave someone Member"])

    async def test_the_audit_log_is_not_waited_on_unless_the_response_asks(self):
        place = FakeChannel(self.guild, "changes")
        await self.make(ROLE, "", "{target}", channel_id=place.id)
        with mock.patch.object(self.bot.audit, "find", mock.AsyncMock()) as find:
            await self.triggers.on_member_update(mock.Mock(roles=[self.guild.default_role]), FakeMember(self.guild, "someone", self.role_member))
        find.assert_not_awaited()

    async def test_a_deleted_channel_is_skipped(self):
        await self.make(ROLE, "", "hi", channel_id=123456789012345678)
        await self.triggers.on_member_update(mock.Mock(roles=[self.guild.default_role]), FakeMember(self.guild, "someone", self.role_member))


class CommandsTest(TriggerBase):
    async def stored(self):
        return await self.triggers.rows(self.guild.id)

    async def test_each_kind_of_create(self):
        for name, kind in [("triggers_create", NORMAL), ("triggers_strict", STRICT), ("triggers_exact", EXACT), ("triggers_startswith", STARTSWITH), ("triggers_endswith", ENDSWITH), ("triggers_channel", CHANNEL), ("triggers_regex", REGEX)]:
            ctx = await self.command(name, f"phrase-{kind}", response="ok")
            self.assertEqual(ctx.fails, [], name)
        rows = {row["match_type"]: row for row in await self.stored()}
        self.assertEqual(set(rows), {NORMAL, STRICT, EXACT, STARTSWITH, ENDSWITH, CHANNEL, REGEX})
        self.assertEqual(rows[CHANNEL]["channel_id"], self.channel.id)
        self.assertEqual(rows[NORMAL]["message"]["content"], "ok")
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "trigger.create")

    async def test_create_refuses_what_can_not_work(self):
        self.assertIn("at least", (await self.command("triggers_create", "x", response="ok")).fails[0])
        self.assertIn("isn't a valid", (await self.command("triggers_regex", "((", response="ok")).fails[0])
        self.assertIn("at most", (await self.command("triggers_create", "x" * 1001, response="ok")).fails[0])
        self.assertIn("at most", (await self.command("triggers_create", "ok", response="x" * 25001)).fails[0])
        await self.command("triggers_create", "Hello", response="ok")
        self.assertIn("already", (await self.command("triggers_create", "hello", response="ok")).fails[0])
        self.assertEqual(len(await self.stored()), 1)

    async def test_role_trigger_defaults_to_this_channel(self):
        place = FakeChannel(self.guild, "other")
        await self.command("triggers_role", response="hello")
        await self.command("triggers_role", response="there", channel=place)
        rows = await self.stored()
        self.assertEqual({row["channel_id"] for row in rows}, {self.channel.id, place.id})
        self.assertEqual({row["match_type"] for row in rows}, {ROLE})

    async def test_remove_by_text_or_id(self):
        first = await self.make(NORMAL, "hello")
        second = await self.make(STRICT, "world")
        await self.command("triggers_remove", "HELLO")
        self.assertEqual([row["id"] for row in await self.stored()], [second])
        ctx = await self.command("triggers_remove", trigger_id=first)
        self.assertIn("No trigger", ctx.fails[0])
        await self.command("triggers_remove", str(second))
        self.assertEqual(await self.stored(), [])

    async def test_remove_asks_for_an_id_when_two_share_the_text(self):
        await self.make(NORMAL, "same")
        await self.make(STRICT, "same")
        self.assertIn("by ID", (await self.command("triggers_remove", "same")).fails[0])

    async def test_clear_asks_first(self):
        await self.make()
        ctx = await self.command("triggers_clear")
        view = ctx.sent[0]["view"]
        self.assertIsInstance(view, ClearView)
        self.assertEqual(len(await self.stored()), 1)
        self.assertEqual(await self.triggers.clear(self.guild.id, self.admin), 1)
        self.assertEqual(await self.stored(), [])
        self.assertIn("no triggers", (await self.command("triggers_clear")).fails[0])

    async def test_role_lists_flip(self):
        trigger_id = await self.make()
        await self.command("triggers_role_blacklist", self.role_mod, trigger_id)
        await self.command("triggers_role_whitelist", self.role_member, trigger_id)
        row = (await self.stored())[0]
        self.assertEqual((row["role_bl"], row["role_wl"]), ([self.role_mod.id], [self.role_member.id]))
        await self.command("triggers_role_blacklist", self.role_mod, trigger_id)
        self.assertEqual((await self.stored())[0]["role_bl"], [])
        self.assertIn("no trigger", (await self.command("triggers_role_whitelist", self.role_mod, 9999)).fails[0])

    async def test_ignore_add_remove_and_unignore(self):
        await self.command("triggers_ignore_add", self.member, self.channel)
        self.assertEqual((self.settings("triggers")["ignored_members"], self.settings("triggers")["ignored_channels"]), ([self.member.id], [self.channel.id]))
        await self.command("triggers_ignore_remove", self.member)
        self.assertEqual(self.settings("triggers")["ignored_members"], [])
        self.assertEqual(self.settings("triggers")["ignored_channels"], [self.channel.id])
        await self.command("triggers_unignore", None, self.channel)
        self.assertEqual(self.settings("triggers")["ignored_channels"], [])
        self.assertIn("Name a member", (await self.command("triggers_ignore_add")).fails[0])

    async def test_list_shows_triggers_and_ignores(self):
        await self.make(NORMAL, "hello")
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [self.channel.id]})
        ctx = await self.command("triggers")
        self.assertIn("hello", ctx.sent[0]["content"])
        self.assertIn(f"<#{self.channel.id}>", ctx.sent[0]["content"])
        self.assertFalse(ctx.sent[0]["allowed_mentions"].roles)

    async def test_every_subcommand_needs_manage_server(self):
        for command in self.triggers.triggers.walk_commands():
            self.assertEqual(command.callback.__required_permissions__, ("manage_guild",), command.qualified_name)

    async def test_the_slash_group_is_hidden_from_members_by_default(self):
        data = self.triggers.triggers.app_command.to_dict(self.bot.tree)
        self.assertEqual(int(data["default_member_permissions"]), discord.Permissions(manage_guild=True).value)


class DashboardTest(TriggerBase):
    def form(self, **changes):
        return {"match_type": NORMAL, "trigger": "hello", "message": {"content": "Hi", "embeds": [], "buttons": []}, **changes}

    async def test_create_list_edit_delete(self):
        client = await self.client()
        response = await client.post(self.path("/triggers"), json=self.form(cooldown=30, role_wl=[str(self.role_mod.id)]))
        self.assertEqual(response.status, 200)
        trigger_id = (await response.json())["id"]
        body = await (await client.get(self.path("/triggers"))).json()
        row = body["triggers"][0]
        self.assertEqual((row["trigger"], row["match_type"], row["cooldown"], row["role_wl"], row["message"]["content"], row["skipped"]), ("hello", NORMAL, 30, [str(self.role_mod.id)], "Hi", False))
        self.assertEqual([t["name"] for t in body["match_types"]], ["Normal", "Strict", "Channel specific", "Exact", "Startswith", "Endswith", "Regex", "Role Assignment"])
        response = await client.put(self.path(f"/triggers/{trigger_id}"), json=self.form(trigger="changed", match_type=STRICT))
        self.assertEqual(response.status, 200)
        await self.say("this is changed now", author=self.mod)
        self.assertEqual(self.said(), ["Hi"], "the message path sees the change at once")
        self.assertEqual((await client.delete(self.path(f"/triggers/{trigger_id}"))).status, 200)
        self.assertEqual(await self.triggers.rows(self.guild.id), [])
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions, ["trigger.remove", "trigger.edit", "trigger.create"])

    async def test_bad_input_names_the_field(self):
        client = await self.client()
        for form, field in [
            (self.form(trigger=""), "trigger"), (self.form(trigger="x"), "trigger"), (self.form(match_type=99), "match_type"), (self.form(match_type=REGEX, trigger="("), "trigger"),
            (self.form(match_type=CHANNEL), "channel_id"), (self.form(match_type=ROLE, trigger=""), "channel_id"), (self.form(message={"content": ""}), "message"), (self.form(cooldown=-1), "cooldown"),
            (self.form(cooldown=86401), "cooldown"), (self.form(role_wl=["x"]), "role_wl"), (self.form(channel_id="x"), "channel_id"), (self.form(nonsense=1), "nonsense"),
        ]:
            response = await client.post(self.path("/triggers"), json=form)
            self.assertEqual(response.status, 400, form)
            self.assertIn(field, (await response.json())["fields"], form)

    async def test_a_repeat_is_refused(self):
        client = await self.client()
        await client.post(self.path("/triggers"), json=self.form())
        response = await client.post(self.path("/triggers"), json=self.form(trigger="HELLO"))
        self.assertIn("trigger", (await response.json())["fields"])

    async def test_a_trigger_in_another_server_can_not_be_reached(self):
        client = await self.client()
        foreign = await self.triggers._insert(999, {"match_type": NORMAL, "trigger": "foreign", "message": {"content": "x", "embeds": [], "buttons": []}, "channel_id": None, "role_wl": [], "role_bl": [], "channel_wl": [], "channel_bl": [], "cooldown": 0}, 1)
        self.assertEqual((await client.put(self.path(f"/triggers/{foreign}"), json=self.form())).status, 404)
        self.assertEqual((await client.delete(self.path(f"/triggers/{foreign}"))).status, 404)
        self.assertEqual(len(await self.triggers.rows(999)), 1)

    async def test_staff_can_not_reach_any_of_it(self):
        client = await self.client(access="staff")
        for method, path in [("get", "/triggers"), ("post", "/triggers"), ("post", "/triggers/import"), ("post", "/triggers/preview"), ("get", "/triggers/export"), ("delete", "/triggers/1")]:
            response = await getattr(client, method)(self.path(path), **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status, 403, path)

    async def test_ignore_settings_save_with_ids_as_text(self):
        client = await self.client()
        response = await client.put(self.path("/settings/triggers"), json={"ignored_channels": [str(self.channel.id)], "ignored_members": [str(self.member.id)]})
        body = await response.json()
        self.assertEqual(body["ignored_channels"], [str(self.channel.id)])
        self.assertEqual(self.settings("triggers")["ignored_members"], [self.member.id])

    async def test_preview(self):
        client = await self.client()
        body = await (await client.post(self.path("/triggers/preview"), json={"args": "hello there", "message": {"content": "You said {args}{reactu:x}"}})).json()
        self.assertEqual(body["message"]["content"], "You said hello there")
        self.assertIn("Reacts", body["does"][0])
        self.assertEqual(self.said(), [])

    async def test_a_skipped_pattern_shows(self):
        client = await self.client()
        await self.make(REGEX, "abc")
        self.triggers.guard._slow_until[(self.guild.id, "abc")] = time.monotonic() + 100
        body = await (await client.get(self.path("/triggers"))).json()
        self.assertTrue(body["triggers"][0]["skipped"])


class ImportExportTest(TriggerBase):
    # The shape another dashboard hands out.
    LEGACY = [
        {"channel": None, "channel_bl": [], "channel_wl": [], "embed": "{}", "guild_id": "970", "id": 1, "match_type": 1, "response": "https://example.com/a.gif", "role_bl": None, "role_wl": None, "roles_bl": [], "roles_wl": [], "specific_channel": None, "trigger": "bambi"},
        {"channel": None, "channel_bl": [], "channel_wl": [], "embed": "{}", "guild_id": "970", "id": 2, "match_type": 2, "response": "Have you checked <#100200300400500600>?", "role_bl": None, "role_wl": None, "roles_bl": None, "roles_wl": None, "specific_channel": None, "trigger": "how do i"},
        {"channel": "100200300400500601", "channel_bl": [], "channel_wl": [], "embed": "{\"title\": \"Role\"}", "guild_id": "970", "id": 3, "match_type": 8, "response": "", "role_bl": None, "role_wl": None, "roles_bl": ["100200300400500602"], "roles_wl": [], "specific_channel": None, "trigger": "role_trigger"},
        {"channel": None, "channel_bl": ["100200300400500603"], "channel_wl": [], "embed": "{}", "guild_id": "970", "id": 4, "match_type": 3, "response": "here", "role_bl": None, "role_wl": None, "roles_bl": [], "roles_wl": [], "specific_channel": "100200300400500604", "trigger": "only here"},
    ]

    async def post(self, client, **form):
        response = await client.post(self.path("/triggers/import"), json=form)
        return response.status, await response.json()

    async def test_the_shape_another_dashboard_exports_fills_our_tables(self):
        client = await self.client()
        status, body = await self.post(client, data={"ignored_channels": ["100200300400500605"], "limit": 50, "triggers": self.LEGACY})
        self.assertEqual((status, body["imported"]), (200, 4))
        rows = {row["match_type"]: row for row in await self.triggers.rows(self.guild.id)}
        self.assertEqual((rows[1]["phrase"], rows[1]["message"]["content"]), ("bambi", "https://example.com/a.gif"))
        self.assertEqual((rows[2]["phrase"], rows[2]["role_wl"], rows[2]["role_bl"]), ("how do i", [], []))
        self.assertEqual((rows[8]["phrase"], rows[8]["channel_id"], rows[8]["role_bl"], rows[8]["message"]["embeds"][0]["title"]), ("", 100200300400500601, [100200300400500602], "Role"))
        self.assertEqual((rows[3]["channel_id"], rows[3]["channel_bl"]), (100200300400500604, [100200300400500603]))
        self.assertEqual(self.settings("triggers")["ignored_channels"], [100200300400500605])
        await self.say("so how do i print")
        self.assertEqual(self.said(), ["Have you checked <#100200300400500600>?"])

    async def test_preview_changes_nothing(self):
        client = await self.client()
        await self.make(NORMAL, "bambi")
        status, body = await self.post(client, preview=True, data=self.LEGACY + [{"match_type": 99, "trigger": "x"}, {"match_type": 7, "trigger": "(", "response": "x"}, self.LEGACY[1]])
        self.assertEqual((status, body["imported"]), (200, 0))
        self.assertEqual([row["action"] for row in body["rows"]], ["skip", "create", "create", "create", "skip", "skip", "skip"])
        self.assertEqual(len(await self.triggers.rows(self.guild.id)), 1)

    async def test_replace(self):
        client = await self.client()
        await self.make(NORMAL, "bambi", "old")
        status, body = await self.post(client, data=self.LEGACY[:1], replace=True)
        self.assertEqual(body["imported"], 1)
        rows = await self.triggers.rows(self.guild.id)
        self.assertEqual((len(rows), rows[0]["message"]["content"]), (1, "https://example.com/a.gif"))

    async def test_export_then_import_is_the_same(self):
        client = await self.client()
        await self.make(STRICT, "hello", "{user}", embeds=[{"title": "T"}], cooldown=9, role_bl=[self.role_mod.id])
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_members": [self.member.id]})
        exported = await (await client.get(self.path("/triggers/export"))).json()
        await self.db.run("DELETE FROM triggers")
        self.triggers._changed(self.guild.id)
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_members": []})
        status, body = await self.post(client, data=json.dumps(exported))
        self.assertEqual((status, body["imported"]), (200, 1))
        row = (await self.triggers.rows(self.guild.id))[0]
        self.assertEqual((row["match_type"], row["phrase"], row["message"]["embeds"], row["cooldown"], row["role_bl"]), (STRICT, "hello", [{"title": "T"}], 9, [self.role_mod.id]))
        self.assertEqual(self.settings("triggers")["ignored_members"], [self.member.id])

    async def test_bad_files(self):
        client = await self.client()
        for data in ("{nope", 5):
            status, body = await self.post(client, data=data)
            self.assertEqual(status, 400)
            self.assertIn("data", body["fields"])
        self.assertEqual((await client.post(self.path("/triggers/import"), json=[1])).status, 400)

    def test_imported_trigger_refuses_what_can_not_work(self):
        for item in (5, {"match_type": [1], "trigger": "xx", "response": "y"}, {"match_type": 1, "trigger": "x" * 1001, "response": "y"}, {"match_type": 0, "trigger": "xx"}, {"match_type": 1, "trigger": "x", "response": "y"}, {"match_type": 1, "trigger": "xx"}, {"match_type": 3, "trigger": "xx", "response": "y"}, {"match_type": 1, "trigger": "xx", "response": "y", "embed": "{no"}):
            with self.assertRaises(Invalid):
                imported_trigger(item)


class RealLibraryTest(GatewayTest):
    """Messages and role changes as Discord sends them, through discord.py."""
    modules = [Tags, Triggers]

    async def make(self, kind=NORMAL, phrase="hello", response="Hi {user}", **changes):
        cog = next(m for m in self.bot.modules if isinstance(m, Triggers))
        form = {"match_type": kind, "trigger": phrase, "message": {"content": response, "embeds": [], "buttons": []}, "channel_id": None, "role_wl": [], "role_bl": [], "channel_wl": [], "channel_bl": [], "cooldown": 0, **changes}
        return await cog._insert(self.guild_id, form, self.owner_id)

    def said(self):
        return [message["content"] for message in self.net.posted]

    async def test_a_message_sets_off_a_trigger(self):
        await self.make()
        self.dispatch_message(user_id=self.member_id, content="oh hello there")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["Hi member"])

    async def test_the_bot_s_own_message_never_does(self):
        await self.make()
        self.dispatch_message(user_id=self.bot_id, content="hello")
        await self.settle()
        self.assertEqual(self.said(), [])

    async def test_a_regex_trigger_through_the_real_loop(self):
        await self.make(REGEX, "hel+o")
        self.dispatch_message(user_id=self.member_id, content="hellllo")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["Hi member"])

    async def test_a_role_change_through_the_gateway(self):
        await self.make(ROLE, "", "{target} now {role(is_added)} {role}", channel_id=int(self.log_channel["id"]))
        member = self.member_json[self.member_id]
        self.bot._connection.parse_guild_member_update({
            "guild_id": str(self.guild_id), "user": member["user"], "roles": [self.role_member["id"], self.role_mod["id"]], "nick": None, "joined_at": member["joined_at"], "deaf": False, "mute": False,
            "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": None,
        })
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["member now true Moderator"])
        self.assertEqual(self.net.posted[0]["channel_id"], self.log_channel["id"])

    async def test_a_role_trigger_s_command_block_has_a_message_to_run_from(self):
        await self.make(ROLE, "", "{cmd:nothing}", channel_id=int(self.log_channel["id"]), trusted=True)
        member = self.member_json[self.member_id]
        self.bot._connection.parse_guild_member_update({
            "guild_id": str(self.guild_id), "user": member["user"], "roles": [self.role_member["id"], self.role_mod["id"]], "nick": None, "joined_at": member["joined_at"], "deaf": False, "mute": False,
            "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": None,
        })
        await self.settle()
        self.assert_no_errors()
        self.assertIn("no command", self.said()[0])
