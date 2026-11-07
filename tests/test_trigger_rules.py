"""What a trigger may read and post for whoever wrote it, how fast it can post, and what the Triggers page says about it."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock


from captainguild.core.tagscript import Scope, run_tag
from captainguild.core.tagscript import runner
from captainguild.modules import triggers as triggers_module
from captainguild.modules.logs import Logs
from captainguild.modules.tags import Tags
from captainguild.modules.triggers import CHANNEL, NORMAL, REGEX, ROLE, Triggers
from tests.fakes import FakeChannel, FakeMember, FakeMessage, FakePermissions
from tests.test_access_limits import HiddenChannel, SiteCase
from tests.test_triggers import TriggerBase


class TriggerSite(SiteCase):
    modules = [Tags, Triggers, Logs]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.triggers = self.cog(Triggers)
        self.addAsyncCleanup(self.triggers.guard.close)
        self.manager_role.permissions = FakePermissions("manage_guild", "view_channel", "read_message_history", "send_messages")

    async def row(self, trigger_id):
        return next(row for row in await self.triggers.rows(self.guild.id) if row["id"] == trigger_id)

    async def make(self, by, kind=NORMAL, phrase="go", content="yes", **changes):
        form = {"match_type": kind, "trigger": phrase, "message": {"content": content, "embeds": [], "buttons": []}, "channel_id": None, "role_wl": [], "role_bl": [], "channel_wl": [], "channel_bl": [], "cooldown": 0, **changes}
        return await self.triggers._insert(self.guild.id, form, by.id)

    def said(self, channel):
        return [message.content for message in channel.sent]


class TrustTest(TriggerSite):
    async def test_moving_a_trusted_trigger_ends_the_trust(self):
        second = FakeChannel(self.guild, "second")
        trigger_id = await self.make(self.admin, CHANNEL, channel_id=self.channel.id, trusted=True, content="{cmd:x}")
        client = await self.site(self.manager)
        answer = await client.put(self.path(f"/triggers/{trigger_id}"), json={"channel_id": str(second.id)})
        self.assertEqual(answer.status, 200, await answer.text())
        row = await self.row(trigger_id)
        self.assertEqual((row["channel_id"], bool(row["trusted"])), (second.id, False))

    async def test_a_save_that_changes_nothing_that_matters_keeps_it(self):
        trigger_id = await self.make(self.admin, CHANNEL, channel_id=self.channel.id, trusted=True, content="{cmd:x}")
        client = await self.site(self.manager)
        await client.put(self.path(f"/triggers/{trigger_id}"), json={"cooldown": 5})
        self.assertTrue((await self.row(trigger_id))["trusted"])

    async def test_an_administrator_can_move_it_and_keep_it(self):
        second = FakeChannel(self.guild, "second")
        trigger_id = await self.make(self.admin, CHANNEL, channel_id=self.channel.id, trusted=True, content="{cmd:x}")
        client = await self.site(self.admin)
        await client.put(self.path(f"/triggers/{trigger_id}"), json={"channel_id": str(second.id)})
        self.assertTrue((await self.row(trigger_id))["trusted"])

    async def test_someone_who_couldn_t_vouch_is_told_trust_went(self):
        trigger_id = await self.make(self.admin, trusted=True, content="{cmd:x}")
        client = await self.site(self.manager)
        answer = await (await client.put(self.path(f"/triggers/{trigger_id}"), json={"message": {"content": "{cmd:y}"}})).json()
        self.assertTrue(answer["trust_removed"])
        answer = await (await client.put(self.path(f"/triggers/{trigger_id}"), json={"cooldown": 2})).json()
        self.assertNotIn("trust_removed", answer)


class HiddenChannelsTest(TriggerSite):
    """An untrusted trigger reads and posts only where whoever wrote it could."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.hidden = HiddenChannel(self.guild, "mod-only", {self.admin.id})

    async def test_it_can_not_copy_text_out_of_a_channel_its_writer_can_t_read(self):
        await self.make(self.manager, phrase="ab", content=f"{{redirect:{self.channel.id}}}{{args}}")
        await self.triggers._handle_message(FakeMessage(self.hidden, self.admin, "ab secret plan"))
        self.assertEqual(self.said(self.channel), [])

    async def test_it_can_not_post_into_a_channel_its_writer_can_t_see(self):
        await self.make(self.manager, phrase="zz", content=f"{{redirect:{self.hidden.id}}}Injected")
        await self.triggers._handle_message(FakeMessage(self.channel, self.member, "zz"))
        self.assertEqual(self.said(self.hidden), [])

    async def test_the_positive_control_in_channels_its_writer_can_reach(self):
        second = FakeChannel(self.guild, "second")
        await self.make(self.manager, phrase="ab", content=f"{{redirect:{second.id}}}{{args}}")
        await self.triggers._handle_message(FakeMessage(self.channel, self.member, "ab fine"))
        self.assertEqual(self.said(second), ["ab fine"])

    async def test_a_trusted_trigger_is_vouched_for(self):
        await self.make(self.manager, phrase="ab", content=f"{{redirect:{self.channel.id}}}{{args}}", trusted=True)
        await self.triggers._handle_message(FakeMessage(self.hidden, self.admin, "ab plan"))
        self.assertEqual(self.said(self.channel), ["ab plan"])

    async def test_it_stops_when_its_writer_has_left_and_the_page_says_so(self):
        gone = FakeMember(self.guild, "gone", self.manager_role)
        trigger_id = await self.make(gone, phrase="ab")
        self.guild.members.remove(gone)
        await self.triggers._handle_message(FakeMessage(self.channel, self.member, "ab"))
        self.assertEqual(self.said(self.channel), [])
        client = await self.site(self.admin)
        rows = (await (await client.get(self.path("/triggers"))).json())["triggers"]
        self.assertTrue(next(row for row in rows if row["id"] == trigger_id)["writer_gone"])

    async def test_a_cache_gap_is_not_someone_leaving(self):
        gone = FakeMember(self.guild, "gap", self.manager_role)
        await self.make(gone, phrase="ab")
        real = self.guild.get_member
        with mock.patch.object(self.guild, "get_member", lambda user_id: None if user_id == gone.id else real(user_id)), mock.patch.object(self.guild, "fetch_member", mock.AsyncMock(return_value=gone)):
            await self.triggers._handle_message(FakeMessage(self.channel, self.member, "ab"))
        self.assertEqual(self.said(self.channel), ["yes"])

    async def test_a_gone_writer_isn_t_looked_up_on_every_message(self):
        gone = FakeMember(self.guild, "gone", self.manager_role)
        await self.make(gone, phrase="ab")
        self.guild.members.remove(gone)
        with mock.patch.object(self.guild, "fetch_member", wraps=self.guild.fetch_member) as fetched:
            await self.triggers._handle_message(FakeMessage(self.channel, self.member, "ab"))
            await self.triggers._handle_message(FakeMessage(self.channel, self.member, "ab again"))
        self.assertEqual(fetched.call_count, 1)

    async def test_a_role_trigger_posts_only_where_its_writer_can(self):
        await self.make(self.manager, ROLE, phrase="", channel_id=self.hidden.id, content="changed")
        before = SimpleNamespace(roles=[])
        after = FakeMember(self.guild, "changing", self.role_member)
        await self.triggers._handle_role_change(before, after)
        self.assertEqual(self.said(self.hidden), [])

    async def test_a_role_command_needs_the_channel_in_reach(self):
        ctx = self.context(author=self.manager)
        await self.triggers.triggers_role.callback(self.triggers, ctx, response="changed", channel=self.hidden)
        self.assertEqual(ctx.fails, ["You can't see that channel."])
        self.assertEqual(await self.triggers.rows(self.guild.id), [])

    async def test_a_channel_it_can_see_and_not_post_in_is_refused(self):
        client = await self.site(self.manager)
        self.manager_role.permissions = FakePermissions("manage_guild", "view_channel")
        answer = await client.post(self.path("/triggers"), json={"match_type": ROLE, "channel_id": str(self.channel.id), "message": {"content": "x"}})
        self.assertEqual(answer.status, 400)
        ctx = self.context(author=self.manager)
        await self.triggers.triggers_role.callback(self.triggers, ctx, response="changed", channel=self.channel)
        self.assertEqual(ctx.fails, ["You can't post in that channel."])

    async def test_an_import_holds_the_same_line(self):
        client = await self.site(self.manager)
        self.manager_role.permissions = FakePermissions("manage_guild", "view_channel")
        data = [{"match_type": ROLE, "channel": str(self.channel.id), "content": "x"}]
        answer = await (await client.post(self.path("/triggers/import"), json={"data": json.dumps(data), "preview": True})).json()
        self.assertEqual(answer["rows"][0]["action"], "skip")


class RoleTriggerPaceTest(TriggerBase):
    async def fire(self, count, role=None):
        for index in range(count):
            member = FakeMember(self.guild, f"m{index}")
            member.roles = [role or self.role_member]
            await self.triggers._handle_role_change(SimpleNamespace(roles=[]), member)

    async def test_a_mass_grant_posts_only_so_much_and_says_so_once(self):
        await self.make(ROLE, "", "{target} changed", channel_id=self.channel.id)
        await self.fire(triggers_module.ROLE_POSTS_PER_MINUTE + 20)
        said = self.said()
        self.assertEqual(len(said), triggers_module.ROLE_POSTS_PER_MINUTE + 1)
        self.assertEqual(len([line for line in said if "limit" in line]), 1)

    async def test_each_trigger_has_its_own_count(self):
        await self.make(ROLE, "", "first", channel_id=self.channel.id)
        await self.make(ROLE, "", "second", channel_id=self.channel.id)
        await self.fire(5)
        self.assertEqual(self.said().count("first"), 5)
        self.assertEqual(self.said().count("second"), 5)

    async def test_the_count_starts_over_with_the_next_minute(self):
        trigger_id = await self.make(ROLE, "", "yes", channel_id=self.channel.id)
        await self.fire(triggers_module.ROLE_POSTS_PER_MINUTE + 2)
        stamps = self.triggers._role_posts[trigger_id]
        for index in range(len(stamps)):
            stamps[index] = time.monotonic() - 61
        self.channel.sent.clear()
        await self.fire(1)
        self.assertEqual(self.said(), ["yes"])

    async def test_a_cooldown_past_the_minute_holds_for_the_same_change(self):
        await self.make(ROLE, "", "yes", channel_id=self.channel.id, cooldown=3600)
        member = FakeMember(self.guild, "cycling")
        member.roles = [self.role_member]
        await self.triggers._handle_role_change(SimpleNamespace(roles=[]), member)
        self.triggers._fired = {key: when - 120 for key, when in self.triggers._fired.items()}
        await self.triggers._handle_role_change(SimpleNamespace(roles=[]), member)
        self.assertEqual(self.said(), ["yes"])


class RoleTriggerActorTest(TriggerBase):
    def entry(self, actor):
        return SimpleNamespace(user=actor)

    async def test_user_stays_the_actor_and_blocks_act_for_the_member(self):
        captured = {}

        async def spy(bot, payload, scope, **kwargs):
            captured["scope"] = scope
            return await run_tag(bot, payload, scope, **kwargs)

        await self.make(ROLE, "", "{user} did it", channel_id=self.channel.id, trusted=True)
        changed = FakeMember(self.guild, "changed")
        changed.roles = [self.role_member]
        with mock.patch.object(self.bot.audit, "find", mock.AsyncMock(return_value=self.entry(self.admin))), mock.patch.object(triggers_module, "run_tag", spy):
            await self.triggers._handle_role_change(SimpleNamespace(roles=[]), changed)
        self.assertEqual(self.said(), ["admin did it"])
        self.assertIs(captured["scope"].member, self.admin)
        self.assertIs(captured["scope"].acting_member, changed)

    async def test_a_template_without_user_acts_for_the_same_member(self):
        captured = {}

        async def spy(bot, payload, scope, **kwargs):
            captured["scope"] = scope
            return await run_tag(bot, payload, scope, **kwargs)

        await self.make(ROLE, "", "plain", channel_id=self.channel.id, trusted=True)
        changed = FakeMember(self.guild, "changed")
        changed.roles = [self.role_member]
        with mock.patch.object(triggers_module, "run_tag", spy):
            await self.triggers._handle_role_change(SimpleNamespace(roles=[]), changed)
        self.assertIs(captured["scope"].acting_member, changed)

    async def test_a_command_block_runs_as_the_member_changed(self):
        ran = []

        async def run_command(bot, text, **kwargs):
            ran.append(kwargs["author"])

        scope = Scope(guild=self.guild, member=self.admin, channel=self.channel, target=self.member, acting_member=self.member)
        with mock.patch("captainguild.core.tagscript.runner._run_command", run_command):
            await run_tag(self.bot, {"content": "{cmd:ping}", "embeds": [], "buttons": []}, scope, trusted=True, staff_wrote=True)
        self.assertEqual(ran, [self.member])


class IgnoreTest(TriggerBase):
    async def test_an_ignored_member_s_role_change_fires_nothing(self):
        await self.make(ROLE, "", "yes", channel_id=self.channel.id)
        changed = FakeMember(self.guild, "changed")
        changed.roles = [self.role_member]
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_members": [changed.id]})
        await self.triggers._handle_role_change(SimpleNamespace(roles=[]), changed)
        self.assertEqual(self.said(), [])
        other = FakeMember(self.guild, "other")
        other.roles = [self.role_member]
        await self.triggers._handle_role_change(SimpleNamespace(roles=[]), other)
        self.assertEqual(self.said(), ["yes"])


class ShortcutsAreCommandsTest(TriggerBase):
    async def test_a_short_name_doesn_t_set_off_a_trigger(self):
        await self.make(NORMAL, "diagnose", "fired")
        with mock.patch.object(self.bot, "get_prefix", mock.AsyncMock(return_value=["!"])):
            await self.say("!diagnose")
            await self.say("!DIAGNOSE")
            self.assertEqual(self.said(), [])
            await self.say("diagnose")
        self.assertEqual(self.said(), ["fired"])


class RegexTwinTest(TriggerBase):
    def form(self, phrase, kind=REGEX):
        return {"match_type": kind, "trigger": phrase, "channel_id": None}

    async def test_a_pattern_differing_by_case_is_another_pattern(self):
        await self.make(REGEX, r"\d+", "digits")
        self.assertIsNone(await self.triggers._twin(self.guild.id, self.form(r"\D+")))
        self.assertIsNotNone(await self.triggers._twin(self.guild.id, self.form(r"\d+")))

    async def test_plain_text_still_ignores_case(self):
        await self.make(NORMAL, "Hello", "x")
        self.assertIsNotNone(await self.triggers._twin(self.guild.id, self.form("hello", NORMAL)))

    async def test_a_file_can_hold_both(self):
        client = await self.client()
        data = [{"match_type": REGEX, "trigger": r"\d+", "content": "a"}, {"match_type": REGEX, "trigger": r"\D+", "content": "b"}, {"match_type": REGEX, "trigger": r"\d+", "content": "c"}]
        answer = await (await client.post(self.path("/triggers/import"), json={"data": json.dumps(data), "preview": True})).json()
        self.assertEqual([row["action"] for row in answer["rows"]], ["create", "create", "skip"])


class RegexLimitTest(TriggerBase):
    async def test_a_server_holds_only_so_many_patterns(self):
        with mock.patch.object(triggers_module, "REGEX_LIMIT", 2):
            for number in range(2):
                await self.make(REGEX, f"a{number}", "x")
            ctx = await self.command("triggers_regex", "a9", response="x")
            self.assertEqual(ctx.fails, [triggers_module.TOO_MANY_PATTERNS])
            client = await self.client()
            response = await client.post(self.path("/triggers"), json={"match_type": REGEX, "trigger": "a8", "message": {"content": "x"}})
            self.assertEqual(response.status, 400)
            response = await client.post(self.path("/triggers"), json={"match_type": NORMAL, "trigger": "a8", "message": {"content": "x"}})
            self.assertEqual(response.status, 200)

    async def test_turning_a_row_into_a_pattern_counts(self):
        with mock.patch.object(triggers_module, "REGEX_LIMIT", 1):
            await self.make(REGEX, "a0", "x")
            other = await self.make(NORMAL, "plain", "x")
            client = await self.client()
            response = await client.put(self.path(f"/triggers/{other}"), json={"match_type": REGEX})
            self.assertEqual(response.status, 400)

    async def test_editing_a_pattern_doesn_t_count_it_twice(self):
        with mock.patch.object(triggers_module, "REGEX_LIMIT", 1):
            mine = await self.make(REGEX, "a0", "x")
            client = await self.client()
            response = await client.put(self.path(f"/triggers/{mine}"), json={"trigger": "a1"})
            self.assertEqual(response.status, 200, await response.text())

    async def test_an_import_stops_at_the_limit(self):
        with mock.patch.object(triggers_module, "REGEX_LIMIT", 1):
            client = await self.client()
            data = [{"match_type": REGEX, "trigger": "a0", "content": "x"}, {"match_type": REGEX, "trigger": "a1", "content": "x"}]
            answer = await (await client.post(self.path("/triggers/import"), json={"data": json.dumps(data), "preview": True})).json()
            self.assertEqual([row["action"] for row in answer["rows"]], ["create", "skip"])


class LongListsTest(TriggerBase):
    async def test_the_list_fits_a_message(self):
        for number in range(45):
            await self.make(NORMAL, f"{number:02}" + "x" * 59, "y")
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [10 ** 18 + n for n in range(100)], "ignored_members": [10 ** 18 + n for n in range(100)]})
        ctx = await self.command("triggers")
        content = ctx.sent[0]["content"]
        self.assertLessEqual(len(content), 2000)
        self.assertIn("more on the dashboard", content)

    async def test_the_ignore_list_fits_a_message(self):
        await self.bot.settings.save(self.guild.id, "triggers", {"ignored_channels": [10 ** 18 + n for n in range(100)], "ignored_members": [10 ** 18 + n for n in range(100)]})
        ctx = await self.command("triggers_ignore")
        content = ctx.sent[0]["content"]
        self.assertLessEqual(len(content), 2000)
        self.assertIn("more on the dashboard", content)

    async def test_a_short_list_says_nothing_extra(self):
        await self.make(NORMAL, "hello", "y")
        ctx = await self.command("triggers")
        self.assertNotIn("more on the dashboard", ctx.sent[0]["content"])


class ProblemBadgeTest(TriggerBase):
    async def test_the_page_shows_what_last_went_wrong_and_a_good_run_clears_it(self):
        trigger_id = await self.make(NORMAL, "hello", "{redirect:nowhere}x")
        await self.say("hello")
        row = next(row for row in await self.triggers.rows(self.guild.id) if row["id"] == trigger_id)
        self.assertIn("doesn't exist", self.triggers._api_row(row)["problem"])
        self.triggers._problems[trigger_id] = ("old", time.time())
        await self.db.run("UPDATE triggers SET content = 'fine' WHERE id = ?", (trigger_id,))
        self.triggers._changed(self.guild.id)
        await self.say("hello")
        row = next(row for row in await self.triggers.rows(self.guild.id) if row["id"] == trigger_id)
        self.assertIsNone(self.triggers._api_row(row)["problem"])

    async def test_an_untrusted_trigger_s_commands_say_why_they_didn_t_run(self):
        trigger_id = await self.make(NORMAL, "hello", "{cmd:ping}Hi")
        await self.say("hello")
        row = next(row for row in await self.triggers.rows(self.guild.id) if row["id"] == trigger_id)
        self.assertEqual(self.triggers._api_row(row)["problem"], runner.NOT_TRUSTED)

    async def test_a_deleted_trigger_leaves_nothing_behind(self):
        trigger_id = await self.make(NORMAL, "hello", "{redirect:nowhere}x")
        await self.say("hello")
        await self.triggers._delete(self.guild.id, trigger_id)
        self.assertNotIn(trigger_id, self.triggers._problems)


class PreviewRoleTest(TriggerSite):
    async def test_a_role_trigger_s_preview_fills_role(self):
        scope_seen = {}
        real = triggers_module.preview_scope

        def spy(ops, guild_id, user_id, **kwargs):
            scope_seen.update(kwargs)
            return real(ops, guild_id, user_id, **kwargs)

        client = await self.site(self.admin)
        with mock.patch.object(triggers_module, "preview_scope", spy):
            await client.post(self.path("/triggers/preview"), json={"message": {"content": "{role}"}, "match_type": ROLE})
        self.assertIsNotNone(scope_seen.get("role_id"))
        scope_seen.clear()
        with mock.patch.object(triggers_module, "preview_scope", spy):
            await client.post(self.path("/triggers/preview"), json={"message": {"content": "{role}"}, "match_type": NORMAL})
        self.assertIsNone(scope_seen.get("role_id"))


class PreviewTest(TriggerBase):
    async def test_the_route_reads_the_trust_switch(self):
        client = await self.client()
        off = await (await client.post(self.path("/triggers/preview"), json={"message": {"content": "{cmd:ping}"}})).json()
        on = await (await client.post(self.path("/triggers/preview"), json={"message": {"content": "{cmd:ping}"}, "trusted": True})).json()
        self.assertIn("doesn't run", " ".join(off["does"]))
        self.assertIn("permissions of the person", " ".join(on["does"]))


class DeletedThingsTest(TriggerBase):
    async def test_the_page_marks_what_no_longer_exists(self):
        role = self.guild.role("Gone", 3)
        place = FakeChannel(self.guild, "gone")
        await self.make(NORMAL, "hello", "x", role_wl=[role.id], channel_wl=[place.id])
        await self.make(ROLE, "", "y", channel_id=place.id)
        self.guild.roles.remove(role)
        self.guild.channels.remove(place)
        rows = [self.triggers._api_row(row) for row in await self.triggers.rows(self.guild.id)]
        by_kind = {row["match_type"]: row for row in rows}
        self.assertEqual((by_kind[NORMAL]["gone_roles"], by_kind[NORMAL]["gone_channels"]), ([role.id], [place.id]))
        self.assertEqual(by_kind[ROLE]["gone_channels"], [place.id])

    async def test_staff_hear_one_line(self):
        role = self.guild.role("Gone", 3)
        await self.make(NORMAL, "hello", "x", role_wl=[role.id])
        logs = mock.Mock()
        with mock.patch.object(self.bot, "module", lambda key: logs if key == "logging" else None):
            await self.triggers.on_guild_role_delete(role)
            await self.triggers.on_guild_role_delete(self.role_muted)
        logs.staff_notice.assert_called_once()

    async def test_a_channel_going_is_heard_too(self):
        place = FakeChannel(self.guild, "gone")
        await self.make(CHANNEL, "hello", "x", channel_id=place.id)
        logs = mock.Mock()
        with mock.patch.object(self.bot, "module", lambda key: logs if key == "logging" else None):
            await self.triggers.on_guild_channel_delete(place)
        self.assertIn("hello", logs.staff_notice.call_args.args[2])


class MemberNamesTest(TriggerBase):
    async def test_many_names_come_in_one_call(self):
        client = await self.client()
        ids = ",".join(str(number) for number in range(1, 6))
        answer = await (await client.get(self.path(f"/members?ids={ids}"))).json()
        self.assertEqual(answer["members"], [])

    async def test_it_refuses_a_flood_and_reads_only_ids(self):
        client = await self.client()
        ids = ",".join(str(number) for number in range(1, 200))
        self.assertEqual((await client.get(self.path(f"/members?ids={ids}"))).status, 400)
        answer = await (await client.get(self.path("/members?ids=abc,,1"))).json()
        self.assertEqual(answer["members"], [])
