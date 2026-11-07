"""Who a tag acts for, what it may post, and what the Tags page says about it."""
from __future__ import annotations

import json
import time
from unittest import mock

import discord

from captainguild.core.outside import OutsideError, Reply
from captainguild.core.tagscript import Scope, run_tag
from captainguild.core.tagscript.engine import Actions
from captainguild.core.tagscript.preview import describe_actions, preview_scope
from captainguild.modules import tags as tags_module
from captainguild.modules.automod import Automod
from captainguild.modules.config import Config
from captainguild.modules.logs import Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.roles import Roles
from captainguild.modules.tags import Tags, clean_name, imported_tag
from captainguild.core.validate import Invalid
from tests.fakes import FakeChannel, FakeMember
from tests.gateway import iso, new_id
from tests.test_access_limits import SiteCase, TagGateway
from tests.test_tags import TagBase


class WhoOwnsTheWordsCommandTest(TagGateway):
    """Command blocks run for a tag's owner, so they must only ever run the words that owner stands behind."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_mod["id"])})

    async def test_rewriting_an_administrator_s_tag_borrows_nothing(self):
        await self.typed("!tag create rules Be nice", self.admin_id)
        await self.typed(f"!tag edit rules {{c:ban {self.member_id} spam}}", self.mod_id)
        tag = await self.tags.find(self.guild_id, "rules")
        self.assertEqual(tag["owner_id"], self.mod_id, "the words now belong to who wrote them")
        self.seen.clear()
        await self.typed("!rules", self.admin_id)
        self.assertEqual([call for call in self.seen if "bans" in call[1]], [])

    async def test_with_ownership_off_a_member_borrows_nothing_either(self):
        await self.bot.settings.save(self.guild_id, "tags", {"ownership": False})
        await self.typed("!tag create rules Be nice", self.admin_id)
        await self.typed(f"!tag append rules {{c:ban {self.member_id} spam}}", self.attacker)
        self.assertEqual((await self.tags.find(self.guild_id, "rules"))["owner_id"], self.attacker)
        self.seen.clear()
        await self.typed("!rules", self.admin_id)
        self.assertEqual([call for call in self.seen if "bans" in call[1]], [])

    async def test_an_administrator_s_edit_leaves_the_owner_alone(self):
        await self.typed("!tag create mine Hello", self.attacker)
        await self.typed("!tag edit mine Hello there", self.admin_id)
        self.assertEqual((await self.tags.find(self.guild_id, "mine"))["owner_id"], self.attacker)


class WhoOwnsTheWordsSiteTest(SiteCase):
    modules = [Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tags = self.cog(Tags)
        await self.tags._insert(self.guild.id, "rules", {"content": "Be nice", "embeds": [], "buttons": []}, owner_id=self.admin.id, trusted=False)
        self.tag = await self.tags.find(self.guild.id, "rules")

    async def tag_owner(self):
        return (await self.tags.find(self.guild.id, "rules"))["owner_id"]

    async def test_a_manager_s_edit_takes_the_tag(self):
        client = await self.site(self.manager)
        response = await client.put(self.path(f"/tags/{self.tag['id']}"), json={"message": {"content": "{c:ban 1 x}"}})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await self.tag_owner(), self.manager.id)

    async def test_changing_who_can_use_it_leaves_the_owner_alone(self):
        client = await self.site(self.manager)
        await client.put(self.path(f"/tags/{self.tag['id']}"), json={"restricted": True})
        self.assertEqual(await self.tag_owner(), self.admin.id)

    async def test_an_administrator_s_edit_leaves_the_owner_alone(self):
        client = await self.site(self.admin)
        await self.tags.db.run("UPDATE tags SET owner_id = ? WHERE id = ?", (self.manager.id, self.tag["id"]))
        await client.put(self.path(f"/tags/{self.tag['id']}"), json={"message": {"content": "changed"}})
        self.assertEqual(await self.tag_owner(), self.manager.id)

    async def test_a_manager_s_import_over_it_takes_the_tag(self):
        client = await self.site(self.manager)
        response = await client.post(self.path("/tags/import"), json={"data": json.dumps([{"name": "rules", "content": "{c:ban 1 x}"}]), "replace": True})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await self.tag_owner(), self.manager.id)

    async def test_an_administrator_s_import_over_it_leaves_the_owner_alone(self):
        client = await self.site(self.admin)
        await self.tags.db.run("UPDATE tags SET owner_id = ? WHERE id = ?", (self.manager.id, self.tag["id"]))
        await client.post(self.path("/tags/import"), json={"data": json.dumps([{"name": "rules", "content": "new"}]), "replace": True})
        self.assertEqual(await self.tag_owner(), self.manager.id)


class TrustNoticeTest(SiteCase):
    modules = [Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tags = self.cog(Tags)
        await self.tags._insert(self.guild.id, "rules", {"content": "{c:ping}", "embeds": [], "buttons": []}, owner_id=self.admin.id, trusted=True)
        self.tag = await self.tags.find(self.guild.id, "rules")

    async def test_someone_who_couldn_t_vouch_is_told_trust_went(self):
        client = await self.site(self.manager)
        answer = await (await client.put(self.path(f"/tags/{self.tag['id']}"), json={"message": {"content": "{c:ping} now"}, "trusted": True})).json()
        self.assertTrue(answer["trust_removed"])

    async def test_a_save_that_keeps_trust_says_nothing(self):
        client = await self.site(self.manager)
        answer = await (await client.put(self.path(f"/tags/{self.tag['id']}"), json={"restricted": True})).json()
        self.assertNotIn("trust_removed", answer)


class ListPageTest(SiteCase):
    modules = [Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tags = self.cog(Tags)
        for index in range(30):
            await self.tags._insert(self.guild.id, f"tag{index:02}", {"content": "x" * 1000, "embeds": [{"title": "T"}] if index == 1 else [], "buttons": []}, owner_id=self.admin.id, trusted=index == 2, uses=index)
        self.client_ = await self.site(self.admin)

    async def listing(self, query=""):
        return await (await self.client_.get(self.path(f"/tags{query}"))).json()

    async def test_a_page_holds_summaries_and_not_whole_messages(self):
        body = await self.listing("?page=1&size=10")
        self.assertEqual((len(body["tags"]), body["total"]), (10, 30))
        row = body["tags"][1]
        self.assertNotIn("message", row)
        self.assertEqual(len(row["summary"]), tags_module.SUMMARY_MAX)
        self.assertEqual((row["name"], row["has_embed"]), ("tag01", True))
        self.assertTrue(body["tags"][2]["trusted"])

    async def test_it_finds_by_name_and_by_text(self):
        await self.tags._insert(self.guild.id, "needle", {"content": "plain", "embeds": [], "buttons": []}, owner_id=self.admin.id, trusted=False)
        await self.tags._insert(self.guild.id, "other", {"content": "holds 100%_ of it", "embeds": [], "buttons": []}, owner_id=self.admin.id, trusted=False)
        self.assertEqual([row["name"] for row in (await self.listing("?q=needle"))["tags"]], ["needle"])
        self.assertEqual([row["name"] for row in (await self.listing("?q=100%25_"))["tags"]], ["other"])
        self.assertEqual((await self.listing("?q=nothing-like-this"))["total"], 0)

    async def test_it_sorts_by_uses(self):
        self.assertEqual((await self.listing("?sort=uses&down=1&size=1"))["tags"][0]["name"], "tag29")
        self.assertEqual((await self.listing("?sort=nonsense&size=1"))["tags"][0]["name"], "tag00")

    async def test_odd_paging_numbers_are_held_in_range(self):
        self.assertEqual(len((await self.listing("?page=-4&size=99999"))["tags"]), 30)
        self.assertEqual((await self.listing("?page=abc&size=abc"))["page"], 1)
        self.assertEqual((await self.listing("?page=99"))["tags"], [])

    async def test_one_tag_comes_whole(self):
        row = await (await self.client_.get(self.path("/tags"))).json()
        one = await (await self.client_.get(self.path(f"/tags/{row['tags'][0]['id']}"))).json()
        self.assertEqual(one["message"]["content"], "x" * 1000)
        self.assertEqual((one["aliases"], one["share_code"]), ([], None))
        self.assertEqual((await self.client_.get(self.path("/tags/999999"))).status, 404)


class TrustFromThePageTest(SiteCase):
    modules = [Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tags = self.cog(Tags)
        for name, content in (("plain", "Hello"), ("runs", "{cmd:ping}"), ("also", "{cmdAdmin:ban 1}")):
            await self.tags._insert(self.guild.id, name, {"content": content, "embeds": [], "buttons": []}, owner_id=self.admin.id, trusted=False)
        self.ids = {row["name"]: row["id"] for row in await self.tags.db.all("SELECT id, name FROM tags")}

    async def test_the_page_lists_tags_whose_commands_wait_on_trust(self):
        client = await self.site(self.admin)
        rows = (await (await client.get(self.path("/tags/waiting"))).json())["tags"]
        self.assertEqual({row["name"]: row["commands"] for row in rows}, {"runs": ["ping"], "also": ["ban 1"]})

    async def test_one_click_trusts_the_ones_named(self):
        client = await self.site(self.admin)
        response = await client.post(self.path("/tags/trust"), json={"ids": [self.ids["runs"], self.ids["also"]]})
        self.assertEqual((await response.json())["trusted"], 2)
        self.assertTrue((await self.tags.find(self.guild.id, "runs"))["trusted"])
        self.assertFalse((await self.tags.find(self.guild.id, "plain"))["trusted"])
        self.assertEqual((await (await client.get(self.path("/tags/waiting"))).json())["tags"], [])
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions.count("tag.trust"), 2)

    async def test_only_the_owner_or_an_administrator_can(self):
        client = await self.site(self.manager)
        response = await client.post(self.path("/tags/trust"), json={"ids": [self.ids["runs"]]})
        self.assertEqual(response.status, 403)
        self.assertFalse((await self.tags.find(self.guild.id, "runs"))["trusted"])

    async def test_another_server_s_ids_do_nothing(self):
        client = await self.site(self.admin)
        await self.tags.db.run("UPDATE tags SET guild_id = 5 WHERE id = ?", (self.ids["runs"],))
        response = await client.post(self.path("/tags/trust"), json={"ids": [self.ids["runs"], "nonsense", -1]})
        self.assertEqual((await response.json())["trusted"], 0)


class ClaimTest(TagBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.gone = FakeMember(self.guild, "gone")
        await self.make("theirs", owner=self.gone, trusted=False)

    async def test_an_owner_the_cache_is_missing_keeps_their_tag(self):
        real = self.guild.get_member
        with mock.patch.object(self.guild, "get_member", lambda user_id: None if user_id == self.gone.id else real(user_id)), mock.patch.object(self.guild, "fetch_member", mock.AsyncMock(return_value=self.gone)):
            ctx = await self.command("tag_claim", "theirs", author=self.member)
        self.assertEqual(ctx.fails, ["That tag's owner is still in the server."])
        self.assertEqual((await self.tags.find(self.guild.id, "theirs"))["owner_id"], self.gone.id)

    async def test_someone_who_really_left_loses_it(self):
        self.guild.members.remove(self.gone)
        ctx = await self.command("tag_claim", "theirs", author=self.member)
        self.assertEqual(ctx.fails, [])
        self.assertEqual((await self.tags.find(self.guild.id, "theirs"))["owner_id"], self.member.id)

    async def test_discord_not_answering_says_nothing_about_leaving(self):
        self.guild.members.remove(self.gone)
        response = mock.Mock(status=500, reason="error")
        with mock.patch.object(self.guild, "fetch_member", side_effect=discord.HTTPException(response, "boom")):
            ctx = await self.command("tag_claim", "theirs", author=self.member)
        self.assertEqual(len(ctx.fails), 1)
        self.assertEqual((await self.tags.find(self.guild.id, "theirs"))["owner_id"], self.gone.id)


class RawTest(TagBase):
    async def test_raw_holds_the_age_gate(self):
        await self.make("adult", "NSFW TEXT", nsfw=True)
        ctx = await self.command("tag_raw", "adult", author=self.member)
        self.assertEqual(ctx.fails, ["This tag only works in channels marked as NSFW."])
        self.assertEqual(ctx.sent, [])
        place = FakeChannel(self.guild, "adults", nsfw=True)
        ctx = await self.command("tag_raw", "adult", author=self.member, channel=place)
        self.assertEqual(ctx.sent[0]["content"], "NSFW TEXT")


class PastebinTest(TagBase):
    def outside(self, reply=None, error=None):
        fake = mock.Mock()
        fake.get = mock.AsyncMock(return_value=reply, side_effect=error)
        fake.close = mock.AsyncMock()
        self.tags.outside = fake
        return fake

    async def test_the_whole_paste_arrives_through_the_capped_reader(self):
        fake = self.outside(Reply(200, b"y" * 24000))
        ctx = await self.command("tag_procreate", "long", content="https://pastebin.com/abc123", author=self.admin)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(len((await self.tags.find(self.guild.id, "long"))["message"]["content"]), 24000)
        self.assertEqual(fake.get.call_args.args[0], "https://pastebin.com/raw/abc123")
        self.assertIn("max_bytes", fake.get.call_args.kwargs)

    async def test_a_body_over_the_cap_is_refused_with_a_sentence(self):
        self.outside(error=OutsideError("too much"))
        ctx = await self.command("tag_procreate", "long", content="https://pastebin.com/abc123", author=self.admin)
        self.assertEqual(ctx.fails, ["I couldn't read that Pastebin link."])

    async def test_a_refused_member_never_makes_the_bot_fetch(self):
        fake = self.outside(Reply(200, b"y"))
        await self.bot.settings.save(self.guild.id, "tags", {"mod_only": True})
        ctx = await self.command("tag_procreate", "long", content="https://pastebin.com/abc123", author=self.member)
        self.assertEqual(ctx.fails, ["Only moderators can manage tags in this server."])
        fake.get.assert_not_called()

    async def test_the_fetch_has_a_rate(self):
        fake = self.outside(Reply(200, b"y"))
        for index in range(tags_module.CREATE_RATE):
            await self.command("tag_procreate", f"long{index}", content="https://pastebin.com/abc123", author=self.member)
        self.assertEqual(fake.get.call_count, tags_module.CREATE_RATE)
        ctx = await self.command("tag_procreate", "onemore", content="https://pastebin.com/abc123", author=self.member)
        self.assertEqual(ctx.fails, [tags_module.TOO_MANY_NEW])
        self.assertEqual(fake.get.call_count, tags_module.CREATE_RATE)


class FillingTheServerTest(TagBase):
    async def test_members_stop_short_of_the_limit_so_staff_can_still_make_tags(self):
        with mock.patch.object(tags_module, "TAG_LIMIT", 10):
            for index in range(8):
                await self.make(f"t{index}")
            self.assertTrue(await self.tags.create(self.guild, self.member, "mine", "x"))
            self.assertIsNone(await self.tags.create(self.guild, self.admin, "staff", "x"))

    async def test_aliases_stop_short_too(self):
        tag = await self.make("target")
        with mock.patch.object(tags_module, "TAG_LIMIT", 10):
            for index in range(8):
                await self.tags.db.run("INSERT INTO tag_aliases (guild_id, name, tag_id, owner_id, created_at) VALUES (?, ?, ?, ?, ?)", (self.guild.id, f"a{index}", tag["id"], self.admin.id, time.time()))
            ctx = await self.command("tag_alias", "mine", "target", author=self.member)
            self.assertTrue(ctx.fails)
            self.assertEqual((await self.command("tag_alias", "staff", "target", author=self.admin)).fails, [])

    async def test_one_member_has_a_rate_of_new_tags(self):
        for index in range(tags_module.CREATE_RATE):
            self.assertIsNone(await self.tags.create(self.guild, self.member, f"m{index}", "x"))
        self.assertEqual(await self.tags.create(self.guild, self.member, "onemore", "x"), tags_module.TOO_MANY_NEW)
        self.assertIsNone(await self.tags.create(self.guild, self.admin, "staffs", "x"), "moderators aren't held to it")

    async def test_the_rate_recovers(self):
        for index in range(tags_module.CREATE_RATE):
            await self.tags.create(self.guild, self.member, f"m{index}", "x")
        stamps = self.tags._created[(self.guild.id, self.member.id)]
        for index in range(len(stamps)):
            stamps[index] = time.monotonic() - tags_module.CREATE_WINDOW - 1
        self.assertIsNone(await self.tags.create(self.guild, self.member, "later", "x"))


class NameTest(TagBase):
    def test_get_can_not_be_a_tag_s_name(self):
        with self.assertRaises(Invalid):
            clean_name("get")
        with self.assertRaises(Invalid):
            clean_name("GET")
        self.assertEqual(clean_name("getter"), "getter")

    async def test_a_command_refuses_it_with_a_sentence(self):
        self.assertTrue(await self.tags.create(self.guild, self.admin, "get", "x"))


class AliasMakerTest(TagBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.make("mine", owner=self.admin, trusted=False)
        await self.command("tag_alias", "junk", "mine", author=self.member)

    async def test_whoever_made_an_alias_can_take_it_off(self):
        ctx = await self.command("tag_remove", "junk", author=self.member)
        self.assertEqual(ctx.fails, [])
        self.assertIsNone(await self.tags.find(self.guild.id, "junk"))
        self.assertIsNotNone(await self.tags.find(self.guild.id, "mine"))

    async def test_nobody_else_can(self):
        other = FakeMember(self.guild, "other", self.role_member)
        ctx = await self.command("tag_remove", "junk", author=other)
        self.assertEqual(ctx.fails, ["That tag belongs to someone else."])
        self.assertIsNotNone(await self.tags.find(self.guild.id, "junk"))

    async def test_making_an_alias_gives_no_claim_on_the_tag(self):
        ctx = await self.command("tag_remove", "mine", author=self.member)
        self.assertEqual(ctx.fails, ["That tag belongs to someone else."])

    async def test_mod_only_still_holds(self):
        await self.bot.settings.save(self.guild.id, "tags", {"mod_only": True})
        ctx = await self.command("tag_remove", "junk", author=self.member)
        self.assertEqual(ctx.fails, ["Only moderators can manage tags in this server."])


class OddImportTest(SiteCase):
    modules = [Tags]

    async def test_one_odd_row_is_skipped_or_kept_and_the_rest_come_in(self):
        client = await self.site(self.admin)
        items = [
            {"name": "urlint", "content": "x", "embeds": [{"title": "t", "image": {"url": 5}}]},
            {"name": "urlbracket", "content": "x", "embeds": [{"title": "t", "image": {"url": "http://[x"}}]},
            {"name": "olddate", "content": "x", "created_at": "0001-01-01T00:00:00"},
            {"name": "fardate", "content": "x", "created_at": "9999-12-31T23:59:59"},
            {"name": "badrfc", "content": "x", "created_at": "Mon, 01 Jan 0001 00:00:00 -0000"},
            {"name": "fine", "content": "x"},
        ]
        response = await client.post(self.path("/tags/import"), json={"data": json.dumps(items), "preview": True})
        self.assertEqual(response.status, 200, await response.text())
        rows = {row["name"]: row["action"] for row in (await response.json())["rows"]}
        self.assertEqual(rows["fine"], "create")
        self.assertEqual(len(rows), 6)

    def test_a_date_in_text_keeps_the_range_a_number_has(self):
        self.assertIsNone(tags_module._when_from("0999-01-01T00:00:00+00:00"))
        self.assertIsNone(tags_module._when_from("9999-12-31T23:59:59+00:00"))
        self.assertEqual(tags_module._when_from("2025-01-01T00:00:00+00:00"), 1735689600.0)
        self.assertEqual(tags_module._when_from("Wed, 01 Jan 2025 00:00:00 +0000"), 1735689600.0)

    def test_a_link_that_cant_be_read_says_so_in_a_sentence(self):
        for url in (5, "http://[x"):
            try:
                imported_tag({"name": "odd", "content": "x", "embeds": [{"title": "t", "image": {"url": url}}]})
            except Invalid:
                pass


class PowerBlockTest(TagBase):
    async def test_any_tag_can_name_a_power_block_since_only_trust_gives_it_power(self):
        for content in ("{override}x", "{cmdAdmin:ping}", "{=(x):cmdadmin}{{x}:ping}"):
            self.assertIsNone(await self.tags.create(self.guild, self.member, f"p{len(content)}", content))
            self.tags._created.clear()


class PreviewWordsTest(SiteCase):
    modules = [Tags]

    def actions(self):
        return Actions(commands=[("ping", False), ("ban 1", True)], override=True, destination="channel", channel_text="general")

    def test_an_untrusted_tag_says_its_commands_are_for_its_owner(self):
        lines = describe_actions(self.actions(), trusted=False, kind="tag")
        joined = "\n".join(lines)
        self.assertNotIn("bot's permissions", joined)
        self.assertNotIn("bot manager role", joined)
        self.assertIn("only for its owner", joined)

    def test_an_untrusted_trigger_says_they_don_t_run(self):
        joined = "\n".join(describe_actions(self.actions(), trusted=False, kind="trigger"))
        self.assertIn("doesn't run", joined)
        self.assertNotIn("bot's permissions", joined)

    def test_trusted_says_what_it_will_really_do(self):
        joined = "\n".join(describe_actions(self.actions(), trusted=True, kind="tag"))
        self.assertIn("with the bot's permissions", joined)
        self.assertIn("with the permissions of the person who ran it", joined)
        self.assertIn("bot manager role", joined)

    async def test_the_route_reads_the_switch(self):
        client = await self.site(self.admin)
        message = {"content": "{cmdAdmin:ping}"}
        off = await (await client.post(self.path("/tags/preview"), json={"message": message})).json()
        on = await (await client.post(self.path("/tags/preview"), json={"message": message, "trusted": True})).json()
        self.assertIn("only for its owner", " ".join(off["does"]))
        self.assertIn("bot's permissions", " ".join(on["does"]))


class PreviewFillsWhatItCanTest(SiteCase):
    modules = [Tags]

    def test_channel_rows_carry_what_blocks_read(self):
        self.channel.topic = "Say hello"
        self.channel.slowmode_delay = 30
        row = self.bot.ops.channel(self.guild.id, self.channel.id)
        self.assertEqual((row["topic"], row["slowmode"]), ("Say hello", 30))

    def test_the_server_s_owner_reads_as_a_name(self):
        scope = preview_scope(self.bot.ops, self.guild.id, self.admin.id)
        self.assertEqual(str(scope.guild.owner), "owner")

    def test_a_thread_row_with_no_topic_is_fine(self):
        self.channel.topic = None
        self.assertIsNone(self.bot.ops.channel(self.guild.id, self.channel.id)["topic"])


class DeletedThingsTest(SiteCase):
    modules = [Tags, Logs]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tags = self.cog(Tags)
        self.gone_role = self.guild.role("Gone", 3)
        self.gone_channel = FakeChannel(self.guild, "gone")
        await self.tags._insert(self.guild.id, "gated", {"content": "x", "embeds": [], "buttons": []}, owner_id=self.admin.id, trusted=False, role_wl=[self.gone_role.id], channel_bl=[self.gone_channel.id])
        await self.tags._insert(self.guild.id, "free", {"content": "x", "embeds": [], "buttons": []}, owner_id=self.admin.id, trusted=False)

    async def test_the_page_marks_what_no_longer_exists_and_keeps_the_id(self):
        self.guild.roles.remove(self.gone_role)
        self.guild.channels.remove(self.gone_channel)
        client = await self.site(self.admin)
        rows = {row["name"]: row for row in (await (await client.get(self.path("/tags"))).json())["tags"]}
        self.assertEqual(rows["gated"]["gone_roles"], [str(self.gone_role.id)] if self.gone_role.id > 2 ** 53 else [self.gone_role.id])
        self.assertEqual(len(rows["gated"]["gone_channels"]), 1)
        self.assertEqual((rows["free"]["gone_roles"], rows["free"]["gone_channels"]), ([], []))
        stored = await self.tags.find(self.guild.id, "gated")
        self.assertEqual(stored["role_wl"], [self.gone_role.id], "an empty whitelist means everyone, so the ID stays")

    async def test_staff_hear_one_line_when_a_named_role_goes(self):
        logs = mock.Mock()
        with mock.patch.object(self.bot, "module", lambda key: logs if key == "logging" else None):
            await self.tags.on_guild_role_delete(self.gone_role)
        logs.staff_notice.assert_called_once()
        self.assertIn("gated", logs.staff_notice.call_args.args[2])

    async def test_a_role_nothing_names_says_nothing(self):
        logs = mock.Mock()
        with mock.patch.object(self.bot, "module", lambda key: logs if key == "logging" else None):
            await self.tags.on_guild_role_delete(self.role_muted)
        logs.staff_notice.assert_not_called()

    async def test_staff_hear_when_a_named_channel_goes(self):
        logs = mock.Mock()
        with mock.patch.object(self.bot, "module", lambda key: logs if key == "logging" else None):
            await self.tags.on_guild_channel_delete(self.gone_channel)
        self.assertIn("gated", logs.staff_notice.call_args.args[2])


class BlockReachTest(TagGateway):
    """A template's text and blocks only reach what its writer could."""
    modules = [Config, Moderation, Roles, Automod, Tags]

    def thread(self, kind=12):
        thread_id = new_id()
        data = {"id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "owner_id": str(self.admin_id), "name": "staff secret", "type": kind, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "last_message_id": None, "flags": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 60, "archive_timestamp": iso(), "locked": False, "invitable": False}}
        self.guild._add_thread(discord.Thread(guild=self.guild, state=self.bot._connection, data=data))
        self.net.channels[thread_id] = data
        return thread_id

    async def test_a_private_thread_is_out_of_a_member_s_reach(self):
        thread_id = self.thread()
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True).value
        await self.typed(f"!tag create leak {{redirect:{thread_id}}}hello from outside", self.attacker)
        self.net.posted.clear()
        await self.typed("!leak", self.attacker)
        self.assertNotIn("hello from outside", self.said())
        self.assertTrue(any("can't post" in (line or "") for line in self.said()))

    async def test_a_public_thread_needs_send_in_threads(self):
        thread_id = self.thread(kind=11)
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True).value
        await self.typed(f"!tag create leak {{redirect:{thread_id}}}hello from outside", self.attacker)
        self.net.posted.clear()
        await self.typed("!leak", self.attacker)
        self.assertNotIn("hello from outside", self.said())
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True).value
        self.net.posted.clear()
        await self.typed("!leak", self.attacker)
        self.assertIn("hello from outside", self.said())

    async def test_the_positive_control_a_channel_the_owner_can_post_in(self):
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True).value
        await self.typed(f"!tag create fine {{redirect:{self.log_channel['id']}}}moved", self.attacker)
        self.net.posted.clear()
        await self.typed("!fine", self.attacker)
        self.assertIn("moved", self.said())

    async def test_a_block_can_call_a_short_name(self):
        await self.typed("!tag create dd {c:diagnose}", self.admin_id)
        self.net.posted.clear()
        await self.typed("!dd", self.admin_id)
        self.assertFalse(any("There is no command" in (line or "") for line in self.said()), self.said())

    async def test_a_short_name_can_not_reach_a_blocked_command(self):
        for name in ("search", "taglist"):
            await self.typed(f"!tag create b{name} {{c:{name} x}}", self.admin_id)
            self.net.posted.clear()
            await self.typed(f"!b{name}", self.admin_id)
            self.assertTrue(any("can't run `tag`" in (line or "") for line in self.said()), self.said())

    async def test_a_real_command_comes_before_a_short_name(self):
        with mock.patch.dict("captainguild.core.shortcuts.SHORTCUTS", {"prefix": "tag search"}):
            await self.typed("!tag create pp {c:prefix list}", self.admin_id)
            self.net.posted.clear()
            await self.typed("!pp", self.admin_id)
        self.assertEqual(self.said()[-1], "Prefixes: `!`, `?`")

    async def test_a_name_nobody_knows_still_says_so(self):
        await self.typed("!tag create nope {c:nothingatall}", self.admin_id)
        self.net.posted.clear()
        await self.typed("!nope", self.admin_id)
        self.assertTrue(any("no command called" in (line or "") for line in self.said()))

    async def test_a_shortcut_word_goes_to_the_command_not_a_member_s_tag(self):
        await self.tags._insert(self.guild_id, "deny", {"content": "Member's answer", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        self.net.posted.clear()
        await self.typed("!deny 5 not now", self.admin_id)
        self.assertNotIn("Member's answer", self.said())

    async def test_a_trusted_tag_holding_that_name_still_answers(self):
        await self.tags._insert(self.guild_id, "deny", {"content": "Staff answer", "embeds": [], "buttons": []}, owner_id=self.admin_id, trusted=True)
        self.net.posted.clear()
        await self.typed("!deny 5 not now", self.admin_id)
        self.assertIn("Staff answer", self.said())

    async def test_a_cmdadmin_run_is_written_down_with_who_and_which_tag(self):
        await self.tags._insert(self.guild_id, "open", {"content": "{cmdAdmin:prefix list}", "embeds": [], "buttons": []}, owner_id=self.owner_id, trusted=True)
        await self.typed("!open", self.attacker)
        rows = [row for row in await self.bot.activity_log.recent(self.guild_id) if row["action"] == "tag.command"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], self.attacker)
        self.assertEqual(rows[0]["detail"], "open: prefix list")

    async def test_an_ordinary_block_writes_nothing_down(self):
        await self.tags._insert(self.guild_id, "open", {"content": "{cmd:prefix list}", "embeds": [], "buttons": []}, owner_id=self.owner_id, trusted=True)
        await self.typed("!open", self.attacker)
        self.assertEqual([row for row in await self.bot.activity_log.recent(self.guild_id) if row["action"] == "tag.command"], [])


class AutomodOnOutputTest(TagGateway):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True).value

    async def test_words_built_at_run_time_meet_the_rule(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["badword"]}})
        await self.typed("!tag create w2 {=(a):bad}{=(b):word}{a}{b}", self.attacker)
        self.assertIsNotNone(await self.tags.find(self.guild_id, "w2"))
        self.net.posted.clear()
        await self.typed("!w2", self.attacker)
        self.assertNotIn("badword", self.said())
        self.assertTrue(any("automod" in (line or "") for line in self.said()))

    async def test_links_meet_the_rule(self):
        await self.bot.settings.save(self.guild_id, "automod", {"links": {"enabled": True, "mode": "blacklist", "blacklist": ["evil.example"]}})
        await self.typed("!tag create l1 https://evil.example/x", self.attacker)
        self.net.posted.clear()
        await self.typed("!l1", self.attacker)
        self.assertEqual([line for line in self.said() if "evil.example" in (line or "")], [])

    async def test_an_embed_s_text_counts(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["badword"]}})
        await self.tags._insert(self.guild_id, "em", {"content": "", "embeds": [{"description": "{=(a):bad}{a}word"}], "buttons": []}, owner_id=self.attacker, trusted=False)
        self.net.posted.clear()
        await self.typed("!em", self.attacker)
        self.assertEqual([post for post in self.net.posted if post.get("embeds")], [])

    async def test_the_person_who_runs_it_stands_for_exemptions(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["badword"]}})
        await self.tags._insert(self.guild_id, "w3", {"content": "{=(a):bad}{a}word", "embeds": [], "buttons": []}, owner_id=self.attacker, trusted=False)
        self.net.posted.clear()
        await self.typed("!w3", self.admin_id)
        self.assertIn("badword", self.said())

    async def test_a_trusted_tag_is_vouched_for(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["badword"]}})
        await self.tags._insert(self.guild_id, "w4", {"content": "badword", "embeds": [], "buttons": []}, owner_id=self.admin_id, trusted=True)
        self.net.posted.clear()
        await self.typed("!w4", self.attacker)
        self.assertIn("badword", self.said())

    async def test_clean_output_goes_out(self):
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["badword"]}})
        await self.typed("!tag create ok fine words", self.attacker)
        self.net.posted.clear()
        await self.typed("!ok", self.attacker)
        self.assertIn("fine words", self.said())


class PingTest(TagBase):
    def scope(self, member=None, target=None, args=""):
        return Scope(guild=self.guild, member=member or self.member, channel=self.channel, target=target, args=args)

    async def sent_mentions(self, content, **kwargs):
        self.channel.sent.clear()
        await run_tag(self.bot, {"content": content, "embeds": [], "buttons": []}, kwargs.pop("scope"), **kwargs)
        return self.channel.sent[-1].kwargs["allowed_mentions"]

    async def test_a_member_s_text_chooses_nobody(self):
        typed = f"<@{self.admin.id}> <@{self.owner.id}>"
        allowed = await self.sent_mentions("{args}", scope=self.scope(args=typed), staff_wrote=True)
        self.assertEqual([user.id for user in allowed.users], [self.member.id])

    async def test_the_person_the_target_and_who_the_author_named_can_be_pinged(self):
        written = f"<@{self.owner.id}> and <@!{self.mod.id}> {{mention}}"
        allowed = await self.sent_mentions(written, scope=self.scope(target=self.admin), staff_wrote=True)
        self.assertEqual({user.id for user in allowed.users}, {self.member.id, self.admin.id, self.owner.id, self.mod.id})
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)

    async def test_a_name_in_an_embed_counts_as_written(self):
        payload = {"content": "x", "embeds": [{"description": f"<@{self.owner.id}>"}], "buttons": []}
        self.channel.sent.clear()
        await run_tag(self.bot, payload, self.scope(), trusted=True)
        self.assertIn(self.owner.id, [user.id for user in self.channel.sent[-1].kwargs["allowed_mentions"].users])

    async def test_discord_s_limit_on_named_people_holds(self):
        written = " ".join(f"<@{100000000000000000 + index}>" for index in range(150))
        allowed = await self.sent_mentions(written, scope=self.scope(), staff_wrote=True)
        self.assertEqual(len(allowed.users), 100)

    async def test_an_untrusted_tag_still_pings_nobody(self):
        allowed = await self.sent_mentions(f"<@{self.owner.id}>", scope=self.scope(), owner_id=self.member.id)
        self.assertFalse(allowed.users)


class RestrictedTagTest(TagGateway):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "commands", {"bot_channels": [int(self.log_channel["id"])]})

    async def bodies(self, typed):
        self.net.calls.clear()
        await self.typed(typed, self.attacker)
        return [call[2] for call in self.net.calls if call[0] == "POST" and call[1].endswith("/messages")]

    async def test_an_untrusted_restricted_tag_pings_only_the_person_it_moved_for(self):
        await self.tags._insert(self.guild_id, "short", {"content": f"hi <@{self.owner_id}>", "embeds": [], "buttons": []}, owner_id=self.admin_id, trusted=False, restricted=True)
        body = (await self.bodies("!short"))[-1]
        self.assertEqual(body["allowed_mentions"]["users"], [str(self.attacker)] if isinstance(body["allowed_mentions"]["users"][0], str) else [self.attacker])
        self.assertEqual(body["allowed_mentions"]["parse"], [])

    async def test_an_untrusted_tag_that_stayed_put_pings_nobody(self):
        await self.tags._insert(self.guild_id, "plain", {"content": "hi", "embeds": [], "buttons": []}, owner_id=self.admin_id, trusted=False)
        body = (await self.bodies("!plain"))[-1]
        self.assertFalse(body["allowed_mentions"].get("users"))

    async def test_a_long_one_still_fits_with_the_mention(self):
        await self.tags._insert(self.guild_id, "long", {"content": "x" * 2000, "embeds": [], "buttons": []}, owner_id=self.admin_id, trusted=False, restricted=True)
        posted = await self.bodies("!long")
        self.assertEqual(len(posted), 1)
        self.assertLessEqual(len(posted[0]["content"]), 2000)
        self.assertTrue(posted[0]["content"].startswith(f"<@{self.attacker}> xxx"))

    async def test_a_command_block_follows_a_restricted_command_to_the_bot_channel(self):
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"prefix": {"restricted": True}}})
        await self.typed("!tag create mine {c:prefix list}", self.attacker)
        self.net.posted.clear()
        await self.typed("!mine", self.attacker)
        answer = next(post for post in self.net.posted if post.get("content") == "Prefixes: `!`, `?`" or "Prefixes" in (post.get("content") or ""))
        self.assertEqual(int(answer["channel_id"]), int(self.log_channel["id"]))
