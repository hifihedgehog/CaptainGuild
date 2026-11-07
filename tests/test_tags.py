import asyncio
import json
from unittest import mock

import discord
from discord import app_commands
from discord.ext import commands

from captainguild.core.module import Module, requires
from captainguild.core.tagscript import MAX_COMMANDS, Scope, run_tag
from captainguild.modules.config import Config
from captainguild.modules.tags import Tags, clean_name, imported_tag
from captainguild.core.validate import Invalid
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember
from tests.gateway import GatewayTest, new_id


class Probe(Module):
    """Commands for a template's command blocks to run."""
    key = "probe"
    calls: list = []

    @commands.command(name="probe")
    async def probe(self, ctx, *, text: str = "") -> None:
        Probe.calls.append((ctx.author.id, text))
        await ctx.send(f"probe {text}".strip())

    @commands.command(name="probeadmin")
    @requires("manage_guild")
    async def probeadmin(self, ctx) -> None:
        Probe.calls.append((ctx.author.id, "admin"))
        await ctx.send("did admin thing")

    @commands.command(name="probeban")
    @requires("ban_members")
    async def probeban(self, ctx) -> None:
        Probe.calls.append((ctx.author.id, "ban"))
        await ctx.send("did ban thing")


class TagBase(BotTest):
    modules = [Tags, Probe]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tags = self.cog(Tags)
        Probe.calls = []

    async def make(self, name="support", content="Hello", *, owner=None, trusted=None, **flags):
        self.tags._recent.clear()  # tests run tags faster than a member is allowed to
        author = owner or self.admin
        await self.tags._insert(self.guild.id, name, {"content": content, "embeds": flags.pop("embeds", []), "buttons": []}, owner_id=author.id, trusted=self.tags.is_mod(author) if trusted is None else trusted, **flags)
        return await self.tags.find(self.guild.id, name)

    async def command(self, name, *args, author=None, channel=None, **kwargs):
        ctx = self.context(author=author, channel=channel)
        await getattr(self.tags, name).callback(self.tags, ctx, *args, **kwargs)
        return ctx


class NamesTest(BotTest):
    def test_rules(self):
        self.assertEqual(clean_name("  SuPPort "), "support")
        self.assertEqual(clean_name(">"), ">")
        for bad in ("", "   ", "two words", "x" * 51, "@everyone", "hi@here", "<@123>", None, 5):
            with self.assertRaises(Invalid):
                clean_name(bad)


class ManagingTest(TagBase):
    async def test_create_records_the_owner_and_activity(self):
        ctx = await self.command("tag_create", "Support", content="Hello there", author=self.member)
        self.assertEqual(ctx.fails, [])
        tag = await self.tags.find(self.guild.id, "support")
        self.assertEqual((tag["owner_id"], tag["message"]["content"], tag["uses"], tag["trusted"]), (self.member.id, "Hello there", 0, False))
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["action"], "tag.create")

    async def test_a_new_tag_starts_untrusted_whoever_wrote_it(self):
        await self.command("tag_create", "a", content="x", author=self.admin)
        self.assertFalse((await self.tags.find(self.guild.id, "a"))["trusted"])

    async def test_create_refuses_bad_names_and_repeats(self):
        await self.make("support")
        for name, words in [("two words", "spaces"), ("SUPPORT", "already"), ("tag", "command")]:
            ctx = await self.command("tag_create", name, content="x")
            self.assertIn(words, ctx.fails[0])
        await self.command("tag_alias", "help2", "support")
        ctx = await self.command("tag_create", "help2", content="x")
        self.assertIn("already", ctx.fails[0])

    async def test_a_member_can_only_own_so_many_tags(self):
        with mock.patch("captainguild.modules.tags.MEMBER_TAG_LIMIT", 2):
            for name in ("a", "b"):
                self.assertEqual((await self.command("tag_create", name, content="x", author=self.member)).fails, [])
            self.assertIn("limit", (await self.command("tag_create", "c", content="x", author=self.member)).fails[0])
            self.assertEqual((await self.command("tag_create", "c", content="x", author=self.admin)).fails, [])

    async def test_content_length(self):
        ctx = await self.command("tag_create", "big", content="x" * 25001)
        self.assertIn("25000", ctx.fails[0])
        self.assertIsNone(await self.tags.find(self.guild.id, "big"))

    async def test_a_power_block_saved_by_a_member_carries_no_power(self):
        """The check on saving could be walked around with a name built by blocks, and it told an Administrator something false. Trust is the thing that guards these."""
        ctx = await self.command("tag_create", "power", content="{override}{cmdAdmin:probeadmin}", author=self.member)
        self.assertEqual(ctx.fails, [])
        tag = await self.tags.find(self.guild.id, "power")
        self.assertFalse(tag["trusted"])
        await run_tag(self.bot, tag["message"], Scope(guild=self.guild, member=self.member, channel=self.channel), owner_id=self.member.id, trusted=tag["trusted"])
        self.assertEqual(Probe.calls, [], "it runs at the owner's own permissions, and a member has no Manage Server")

    async def test_ownership_stops_other_members_but_not_owners_or_moderators(self):
        mine = FakeMember(self.guild, "mine", self.role_member)
        await self.make("x", "one", owner=mine)
        for name, args, kwargs in [("tag_edit", ("x",), {"content": "two"}), ("tag_append", ("x",), {"content": "two"}), ("tag_remove", ("x",), {}), ("tag_replace", ("x", "one"), {"to_text": "two"}), ("tag_restrict", ("x",), {})]:
            ctx = await self.command(name, *args, author=self.member, **kwargs)
            self.assertIn("belongs", ctx.fails[0], name)
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["message"]["content"], "one")
        self.assertEqual((await self.command("tag_edit", "x", content="by owner", author=mine)).fails, [])
        self.assertEqual((await self.command("tag_edit", "x", content="by admin", author=self.admin)).fails, [])
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["message"]["content"], "by admin")

    async def test_ownership_off_lets_members_edit_and_the_power_goes_away(self):
        await self.command("tag_ownership", True, author=self.admin)
        await self.make("x", "{override}{cmd:probeadmin}", owner=self.admin)
        self.assertTrue((await self.tags.find(self.guild.id, "x"))["trusted"])
        await self.command("tag_ownership", False, author=self.admin)
        self.assertEqual((await self.command("tag_edit", "x", content="plain", author=self.member)).fails, [])
        self.assertFalse((await self.tags.find(self.guild.id, "x"))["trusted"])

    async def test_a_member_can_not_keep_a_trusted_tag_s_power_by_appending(self):
        await self.bot.settings.save(self.guild.id, "tags", {"ownership": False})
        await self.make("x", "{cmd:probeadmin}", owner=self.admin)
        await self.command("tag_append", "x", content="more", author=self.member)
        tag = await self.tags.find(self.guild.id, "x")
        self.assertEqual(tag["message"]["content"], "{cmd:probeadmin}\nmore")
        self.assertFalse(tag["trusted"])

    async def test_a_member_s_edit_with_ownership_off_makes_the_tag_theirs(self):
        await self.bot.settings.save(self.guild.id, "tags", {"ownership": False})
        await self.make("x", "plain", owner=self.admin)
        self.assertEqual((await self.command("tag_edit", "x", content="{override}", author=self.member)).fails, [])
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["owner_id"], self.member.id)

    async def test_mod_only_stops_members_from_managing_but_not_using(self):
        await self.command("tag_modonly", True, author=self.admin)
        ctx = await self.command("tag_create", "x", content="y", author=self.member)
        self.assertIn("moderators", ctx.fails[0])
        await self.make("x", "Hello", owner=self.admin)
        ctx = await self.command("tag_remove", "x", author=self.member)
        self.assertIn("moderators", ctx.fails[0])
        ctx = await self.command("tag_alias", "y", "x", author=self.member)
        self.assertIn("moderators", ctx.fails[0])
        ctx = await self.command("tag", "x", author=self.member)
        self.assertEqual(ctx.sent[0]["content"], "Hello")

    async def test_the_bot_manager_role_and_a_bypass_role_count_as_moderators(self):
        self.assertFalse(self.tags.is_mod(self.member))
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": self.role_member.id})
        self.assertTrue(self.tags.is_mod(self.member))
        await self.bot.settings.save(self.guild.id, "commands", {"mod_role": None, "overwrites": {"tag": {"bypass_roles": [self.role_member.id]}}})
        self.assertTrue(self.tags.is_mod(self.member))

    async def test_edit_append_and_replace(self):
        await self.make("x", "one one")
        await self.command("tag_edit", "x", content="red red")
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["message"]["content"], "red red")
        await self.command("tag_append", "x", content="blue")
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["message"]["content"], "red red\nblue")
        await self.command("tag_replace", "x", "red", to_text="green")
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["message"]["content"], "green green\nblue")
        ctx = await self.command("tag_replace", "x", "absent", to_text="y")
        self.assertIn("isn't in", ctx.fails[0])
        ctx = await self.command("tag_replace", "x", "e", to_text="z" * 9000)
        self.assertIn("longer", ctx.fails[0])
        await self.command("tag_replace", "x", "blue")
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["message"]["content"], "green green\n")

    async def test_commands_on_a_missing_tag_say_so(self):
        for name, args, kwargs in [("tag_edit", ("none",), {"content": "x"}), ("tag_raw", ("none",), {}), ("tag_info", ("none",), {}), ("tag_remove", ("none",), {}), ("tag_claim", ("none",), {}), ("tag_share", ("none",), {}), ("tag", ("none",), {})]:
            ctx = await self.command(name, *args, **kwargs)
            self.assertIn("no tag called", ctx.fails[0], name)

    async def test_aliases(self):
        tag = await self.make("support")
        ctx = await self.command("tag_alias", "help", "support", author=self.member)
        self.assertEqual(ctx.fails, [], "ownership doesn't stop aliases")
        self.assertEqual((await self.tags.find(self.guild.id, "HELP"))["id"], tag["id"])
        await self.command("tag_alias", "support", "help2")
        self.assertEqual((await self.tags.find(self.guild.id, "help2"))["id"], tag["id"], "either order works")
        await self.command("tag_edit", "help", content="Changed")
        self.assertEqual((await self.tags.find(self.guild.id, "support"))["message"]["content"], "Changed")
        self.assertIn("command", (await self.command("tag_alias", "tag", "support")).fails[0])
        self.assertIn("already", (await self.command("tag_alias", "help2", "support")).fails[0])
        self.assertIn("no tag", (await self.command("tag_alias", "a", "b")).fails[0])

    async def test_removing_an_alias_leaves_the_tag_and_removing_the_tag_takes_its_aliases(self):
        await self.make("support")
        await self.command("tag_alias", "help", "support")
        await self.command("tag_alias", "other", "support")
        await self.command("tag_remove", "help")
        self.assertIsNone(await self.tags.find(self.guild.id, "help"))
        self.assertIsNotNone(await self.tags.find(self.guild.id, "support"))
        await self.command("tag_remove", "support")
        self.assertIsNone(await self.tags.find(self.guild.id, "support"))
        self.assertIsNone(await self.tags.find(self.guild.id, "other"))

    async def test_raw_shows_the_source(self):
        await self.make("x", "**bold** {user} @everyone")
        ctx = await self.command("tag_raw", "x")
        self.assertIn("\\*\\*bold\\*\\* {user}", ctx.sent[0]["content"])
        self.assertEqual(ctx.sent[0]["allowed_mentions"].everyone, False)

    async def test_raw_uses_a_file_for_long_text_or_embeds(self):
        await self.make("long", "x" * 3000)
        ctx = await self.command("tag_raw", "long")
        self.assertIsInstance(ctx.sent[0]["file"], discord.File)
        await self.make("embed", "text", embeds=[{"title": "T"}])
        ctx = await self.command("tag_raw", "embed")
        body = ctx.sent[0]["file"].fp.read().decode()
        self.assertIn("text", body)
        self.assertIn('"title": "T"', body)

    async def test_info(self):
        await self.make("x")
        await self.make("y")
        await self.db.run("UPDATE tags SET uses = 9 WHERE name = 'y'")
        await self.command("tag_alias", "z", "x")
        ctx = await self.command("tag_info", "x")
        embed = ctx.sent[0]["embed"].to_dict()
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual((embed["title"], fields["Owner"], fields["Uses"], fields["Rank"], fields["Aliases"], fields["Flags"]), ("x", f"<@{self.admin.id}>", "0", "2", "z", "None"))

    async def test_nsfw_and_restrict_flip(self):
        await self.make("x")
        await self.command("tag_nsfw", "x")
        await self.command("tag_restrict", "x")
        tag = await self.tags.find(self.guild.id, "x")
        self.assertTrue(tag["nsfw"] and tag["restricted"])
        await self.command("tag_nsfw", "x")
        await self.command("tag_restrict", "x")
        tag = await self.tags.find(self.guild.id, "x")
        self.assertFalse(tag["nsfw"] or tag["restricted"])

    async def test_permissions_on_commands(self):
        needs = {command.name: command.callback.__required_permissions__ for command in self.tags.tag.commands if hasattr(command.callback, "__required_permissions__")}
        self.assertEqual(needs, {name: ("manage_guild",) for name in ("nsfw", "ownership", "modonly", "share", "unshare", "unshareall")})

    async def test_claim_only_from_someone_who_left(self):
        gone = FakeMember(self.guild, "gone")
        tag = await self.make("x", owner=gone)
        ctx = await self.command("tag_claim", "x", author=self.member)
        self.assertIn("still in", ctx.fails[0])
        self.guild.members.remove(gone)
        ctx = await self.command("tag_claim", "x", author=self.member)
        self.assertEqual(ctx.fails, [])
        tag = await self.tags.find(self.guild.id, "x")
        self.assertEqual((tag["owner_id"], tag["trusted"]), (self.member.id, False))

    async def test_list_and_search(self):
        await self.make("alpha", owner=self.admin)
        await self.make("beta_one", owner=self.member)
        await self.make("beta%two", owner=self.member)
        ctx = await self.command("tag_list")
        self.assertEqual(ctx.sent[0]["content"], "alpha, beta%two, beta\\_one")
        ctx = await self.command("tag_list", member=self.member)
        self.assertEqual(ctx.sent[0]["content"], "beta%two, beta\\_one")
        self.assertEqual((await self.command("tag_search", query="%")).sent[0]["content"], "beta%two")
        self.assertEqual((await self.command("tag_search", query="_")).sent[0]["content"], "beta\\_one")
        self.assertIn("No tag", (await self.command("tag_search", query="zzz")).sent[0]["content"])

    async def test_a_long_list_goes_in_a_file(self):
        for index in range(120):
            await self.make(f"a-rather-long-tag-name-{index}")
        ctx = await self.command("tag_list")
        self.assertIn("120 tags", ctx.sent[0]["content"])
        self.assertIsInstance(ctx.sent[0]["file"], discord.File)

    async def test_stats(self):
        await self.make("a", owner=self.admin)
        await self.make("b", owner=self.member)
        await self.db.run("UPDATE tags SET uses = 5 WHERE name = 'b'")
        fields = {f["name"]: f["value"] for f in (await self.command("tag_stats")).sent[0]["embed"].to_dict()["fields"]}
        self.assertEqual((fields["Tags"], fields["Uses"]), ("2", "5"))
        self.assertTrue(fields["Most used"].startswith("`b`: 5"))
        fields = {f["name"]: f["value"] for f in (await self.command("tag_stats", member=self.admin)).sent[0]["embed"].to_dict()["fields"]}
        self.assertEqual((fields["Tags"], fields["Uses"]), ("1", "0"))

    async def test_procreate_reads_pastebin_or_takes_the_text(self):
        with mock.patch.object(self.tags, "_fetch_pastebin", mock.AsyncMock(return_value="from the paste")) as fetch:
            await self.command("tag_procreate", "pasted", content="https://pastebin.com/AbC123")
        fetch.assert_awaited_once_with("AbC123")
        self.assertEqual((await self.tags.find(self.guild.id, "pasted"))["message"]["content"], "from the paste")
        with mock.patch.object(self.tags, "_fetch_pastebin", mock.AsyncMock(return_value=None)):
            ctx = await self.command("tag_procreate", "bad", content="https://pastebin.com/raw/none")
        self.assertIn("couldn't read", ctx.fails[0])
        await self.command("tag_procreate", "plain", content="just text")
        self.assertEqual((await self.tags.find(self.guild.id, "plain"))["message"]["content"], "just text")

    async def test_share_and_unshare(self):
        await self.make("x")
        ctx = await self.command("tag_share", "x")
        code = (await self.db.one("SELECT code FROM tag_shares"))["code"]
        self.assertIn(code, ctx.sent[0]["content"])
        data = json.loads(ctx.sent[0]["file"].fp.read())
        self.assertEqual(data["tags"][0]["name"], "x")
        await self.command("tag_share", "x")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tag_shares"), 1, "sharing twice keeps one code")
        await self.command("tag_unshare", "x")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tag_shares"), 0)
        self.assertIn("isn't shared", (await self.command("tag_unshare", "x")).fails[0])
        await self.command("tag_share", "x")
        ctx = await self.command("tag_unshareall")
        self.assertIn("1 share code", ctx.oks[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tag_shares"), 0)

    async def test_removing_a_tag_stops_its_share_code(self):
        await self.make("x")
        await self.command("tag_share", "x")
        await self.command("tag_remove", "x")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tag_shares"), 0)

    async def test_prompt_needs_a_slash_command(self):
        self.assertIn("/tag prompt", (await self.command("tag_prompt")).fails[0])

    async def test_autocomplete(self):
        await self.make("support")
        await self.make("supplies")
        await self.make("other")
        await self.command("tag_alias", "suppose", "other")
        await self.db.run("UPDATE tags SET uses = 5 WHERE name = 'supplies'")
        interaction = mock.Mock(guild_id=self.guild.id)
        names = [choice.value for choice in await self.tags.complete_name(interaction, "sUp")]
        self.assertEqual(names, ["supplies", "support", "suppose"])
        interaction.guild_id = 123
        self.assertEqual(await self.tags.complete_name(interaction, "sup"), [])

    async def test_the_slash_commands_offer_autocomplete(self):
        data = self.tags.tag.app_command.to_dict(self.bot.tree)
        get = next(option for option in data["options"] if option["name"] == "get")
        self.assertTrue(next(option for option in get["options"] if option["name"] == "name")["autocomplete"])
        edit = next(option for option in data["options"] if option["name"] == "edit")
        self.assertTrue(next(option for option in edit["options"] if option["name"] == "name")["autocomplete"])
        self.assertEqual({option["name"] for option in data["options"]} >= {
            "get", "create", "remove", "procreate", "append", "alias", "raw", "edit", "nsfw", "restrict", "info", "ownership", "modonly", "claim", "replace", "list", "share", "unshare", "unshareall",
        }, True)

    async def test_a_tag_in_another_server_is_out_of_reach(self):
        await self.make("x")
        other = FakeGuild("Other")
        self.assertIsNone(await self.tags.find(other.id, "x"))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM tags WHERE guild_id = ?", (other.id,)), 0)


class RunningTest(TagBase):
    async def run_tag(self, content, *, author=None, channel=None, args="", embeds=None, **flags):
        tag = await self.make("x", content, embeds=embeds or [], **flags)
        ctx = self.context(author=author or self.member, channel=channel)
        await self.tags.use_tag(ctx, tag, args)
        return ctx

    async def test_a_plain_tag_posts_and_counts(self):
        ctx = await self.run_tag("Hello {user}, use {uses}", args="anything")
        self.assertEqual(ctx.sent[0]["content"], "Hello member, use 1")
        self.assertEqual(ctx.sent[0]["allowed_mentions"].everyone, False)
        self.assertEqual(ctx.sent[0]["allowed_mentions"].roles, False)
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["uses"], 1)

    async def test_an_embed_only_tag_posts_its_embed(self):
        ctx = await self.run_tag("", embeds=[{"title": "{user}"}])
        self.assertIsNone(ctx.sent[0]["content"])
        self.assertEqual(ctx.sent[0]["embeds"][0].title, "member")

    async def test_nothing_to_post_posts_nothing(self):
        self.assertEqual((await self.run_tag("{=(x):1}")).sent, [])

    async def test_args_and_target(self):
        ctx = await self.run_tag("{args}|{target}|{target(id)}", args=f"hi <@{self.mod.id}>")
        self.assertEqual(ctx.sent[0]["content"], f"hi <@{self.mod.id}>|mod|{self.mod.id}")

    async def test_nobody_mentioned_means_the_target_is_the_user(self):
        self.assertEqual((await self.run_tag("{target}")).sent[0]["content"], "member")

    async def test_nsfw_holds_even_for_moderators(self):
        ctx = await self.run_tag("x", nsfw=True, author=self.admin)
        self.assertIn("NSFW", ctx.fails[0])
        self.assertEqual(ctx.sent, [])
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["uses"], 0)
        spicy = FakeChannel(self.guild, "spicy", nsfw=True)
        ctx = self.context(author=self.member, channel=spicy)
        await self.tags.use_tag(ctx, await self.tags.find(self.guild.id, "x"), "")
        self.assertEqual(ctx.sent[0]["content"], "x")

    async def test_channel_lists(self):
        allowed = FakeChannel(self.guild, "allowed")
        ctx = await self.run_tag("x", channel_wl=[allowed.id])
        self.assertIn("this channel", ctx.fails[0])
        tag = await self.tags.find(self.guild.id, "x")
        ctx = self.context(author=self.member, channel=allowed)
        await self.tags.use_tag(ctx, tag, "")
        self.assertEqual(len(ctx.sent), 1)
        await self.db.run("UPDATE tags SET channel_wl = '[]', channel_bl = ?", (json.dumps([self.channel.id]),))
        ctx = self.context(author=self.member)
        await self.tags.use_tag(ctx, await self.tags.find(self.guild.id, "x"), "")
        self.assertIn("this channel", ctx.fails[0])

    async def test_a_category_in_a_list_covers_its_channels(self):
        category = FakeChannel(self.guild, "cat", discord.ChannelType.category)
        inside = FakeChannel(self.guild, "inside", category=category)
        tag = await self.make("x", "ok", channel_wl=[category.id])
        ctx = self.context(author=self.member, channel=inside)
        await self.tags.use_tag(ctx, tag, "")
        self.assertEqual(len(ctx.sent), 1)

    async def test_role_lists_and_moderators_skip_them(self):
        ctx = await self.run_tag("x", role_wl=[self.role_mod.id])
        self.assertIn("role", ctx.fails[0])
        tag = await self.tags.find(self.guild.id, "x")
        ctx = self.context(author=self.mod)
        await self.tags.use_tag(ctx, tag, "")
        self.assertEqual(len(ctx.sent), 1)
        await self.db.run("UPDATE tags SET role_wl = '[]', role_bl = ?", (json.dumps([self.role_member.id]),))
        tag = await self.tags.find(self.guild.id, "x")
        ctx = self.context(author=self.member)
        await self.tags.use_tag(ctx, tag, "")
        self.assertIn("role", ctx.fails[0])
        ctx = self.context(author=self.admin)
        await self.tags.use_tag(ctx, tag, "")
        self.assertEqual(len(ctx.sent), 1)

    async def test_restricted_sends_members_to_the_bot_channel(self):
        bot_channel = FakeChannel(self.guild, "bots")
        await self.bot.settings.save(self.guild.id, "commands", {"bot_channels": [bot_channel.id]})
        ctx = await self.run_tag("x", restricted=True)
        self.assertEqual(ctx.decision.restricted_channel, bot_channel.id)
        ctx = self.context(author=self.admin)
        await self.tags.use_tag(ctx, await self.tags.find(self.guild.id, "x"), "")
        self.assertIsNone(ctx.decision)
        ctx = self.context(author=self.member, channel=bot_channel)
        await self.tags.use_tag(ctx, await self.tags.find(self.guild.id, "x"), "")
        self.assertIsNone(ctx.decision)

    async def test_a_bot_category_counts_for_every_channel_in_it(self):
        category = FakeChannel(self.guild, "bots", kind=discord.ChannelType.category)
        inside = FakeChannel(self.guild, "chat", category=category)
        await self.bot.settings.save(self.guild.id, "commands", {"bot_channels": [category.id]})
        tag = await self.make("x", restricted=True)
        ctx = self.context(author=self.member, channel=inside)
        await self.tags.use_tag(ctx, tag, "")
        self.assertIsNone(ctx.decision)
        ctx = self.context(author=self.member)
        await self.tags.use_tag(ctx, tag, "")
        self.assertEqual(ctx.decision.restricted_channel, category.id)

    async def test_a_burst_from_one_member_is_cut_off(self):
        tag = await self.make("x")
        posted = 0
        for _ in range(6):
            ctx = self.context(author=self.member)
            await self.tags.use_tag(ctx, tag, "")
            posted += len(ctx.sent)
        self.assertEqual(posted, 3)
        ctx = self.context(author=self.mod)
        await self.tags.use_tag(ctx, tag, "")
        self.assertEqual(len(ctx.sent), 1, "another member isn't held back")

    async def test_delete_and_react(self):
        ctx = await self.run_tag("hi{delete}")
        self.assertTrue(ctx.message.deleted)
        ctx = self.context(author=self.mod)
        sent = []

        async def send(**kwargs):
            message = await type(ctx).send(ctx, **kwargs)
            sent.append(message)
            return message

        ctx.send = send
        await self.tags.use_tag(ctx, await self._fresh("{react:a <:b:1> :laughing:}{reactu:c}"), "")
        self.assertEqual(sent[0].added_reactions, ["a", "<:b:1>", "\N{SMILING FACE WITH OPEN MOUTH AND TIGHTLY-CLOSED EYES}"])
        self.assertEqual(ctx.message.added_reactions, ["c"])

    async def _fresh(self, content):
        await self.db.run("DELETE FROM tags")
        return await self.make("x", content + "text")

    async def test_a_custom_emoji_by_name_comes_from_the_server(self):
        emoji = mock.Mock()
        emoji.name = "wave2"
        emoji.__str__ = lambda self: "<:wave2:5>"
        self.guild.emojis = [emoji]
        ctx = await self.run_tag("text{reactu: :wave2:}")
        self.assertEqual(ctx.message.added_reactions, ["<:wave2:5>"])

    async def test_a_reaction_discord_refuses_does_not_stop_the_tag(self):
        ctx = self.context(author=self.member)
        ctx.message.add_reaction = mock.AsyncMock(side_effect=discord.HTTPException(mock.Mock(status=400, reason="bad"), "Unknown Emoji"))
        await self.tags.use_tag(ctx, await self.make("x", "text{reactu:nope}"), "")
        self.assertEqual(ctx.sent[0]["content"], "text")

    async def test_delete_beats_reacting_to_the_same_message(self):
        ctx = await self.run_tag("{delete}{reactu:a}text")
        self.assertTrue(ctx.message.deleted)
        self.assertEqual(ctx.message.added_reactions, [])

    async def test_dm_goes_to_the_person_who_ran_it_only(self):
        ctx = await self.run_tag("secret{dm}", args=f"<@{self.mod.id}>")
        self.assertEqual(ctx.sent, [])
        self.assertEqual(self.member.dms[0]["content"], "secret")
        self.assertEqual(self.mod.dms, [])

    async def test_closed_dms_are_reported(self):
        self.member.send = mock.AsyncMock(side_effect=discord.Forbidden(mock.Mock(status=403, reason="no"), "closed"))
        ctx = await self.run_tag("secret{dm}")
        self.assertIn("closed DMs", ctx.fails[0])

    async def test_redirect_by_every_form(self):
        place = FakeChannel(self.guild, "elsewhere")
        for form in (place.name, f"#{place.name}", str(place.id), f"<#{place.id}>"):
            await self.db.run("DELETE FROM tags")
            place.sent.clear()
            ctx = await self.run_tag("moved{redirect:%s}" % form, owner=self.admin)
            self.assertEqual(ctx.sent, [])
            self.assertEqual(place.sent[0].content, "moved", form)

    async def test_redirect_needs_the_owner_to_be_able_to_post_unless_a_moderator_wrote_it(self):
        place = FakeChannel(self.guild, "elsewhere")
        ctx = await self.run_tag("moved{redirect:elsewhere}", owner=self.member)
        self.assertIn("can't post", ctx.fails[0])
        self.assertEqual(place.sent, [])

    async def test_redirect_to_a_channel_in_another_server_is_no_channel(self):
        other = FakeGuild("Other")
        away = FakeChannel(other, "away")
        ctx = await self.run_tag("moved{redirect:%d}" % away.id, owner=self.admin)
        self.assertIn("doesn't exist", ctx.fails[0])
        self.assertEqual(away.sent, [])

    async def test_require(self):
        ctx = await self.run_tag("{require(you're not cool enough):Moderator}Welcome")
        self.assertEqual(ctx.sent[0]["content"], "you're not cool enough")
        self.assertEqual(ctx.sent[0]["allowed_mentions"].users, False)
        self.assertNotIn("Welcome", [s["content"] for s in ctx.sent])
        ctx = await self.run_tag_as("{require(no):Moderator}Welcome", self.mod)
        self.assertEqual(ctx.sent[0]["content"], "Welcome")

    async def run_tag_as(self, content, author, channel=None):
        await self.db.run("DELETE FROM tags")
        tag = await self.make("x", content)
        ctx = self.context(author=author, channel=channel)
        await self.tags.use_tag(ctx, tag, "")
        return ctx

    async def test_require_without_a_message_reacts(self):
        ctx = await self.run_tag("{require:Moderator}{cmd:probe}Welcome")
        self.assertEqual(ctx.sent, [])
        self.assertEqual(ctx.message.added_reactions, ["\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"])
        self.assertEqual(Probe.calls, [], "a refused tag runs nothing")

    async def test_require_accepts_channels_ids_and_mentions(self):
        for entry in ("general", "#general", str(self.channel.id), f"<#{self.channel.id}>", "Member", str(self.role_member.id), f"<@&{self.role_member.id}>", "member"):
            ctx = await self.run_tag_as("{require(no):elsewhere, %s}ok" % entry, self.member)
            self.assertEqual(ctx.sent[0]["content"], "ok", entry)

    async def test_blacklist(self):
        ctx = await self.run_tag_as("{blacklist(not you):Member}ok", self.member)
        self.assertEqual(ctx.sent[0]["content"], "not you")
        self.assertEqual((await self.run_tag_as("{blacklist:Moderator}ok", self.member)).sent[0]["content"], "ok")
        ctx = await self.run_tag_as("{blacklist:general}ok", self.member)
        self.assertEqual(ctx.sent, [])
        self.assertEqual(ctx.message.added_reactions, ["\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"])

    async def test_a_template_that_does_too_much_is_stopped_with_a_message(self):
        ctx = await self.run_tag("{=(x):a}" + "{=(x):{x}{x}}" * 60 + "{x}{cmd:probe}{delete}")
        self.assertIn("too much", ctx.fails[0])
        self.assertEqual(ctx.sent, [])
        self.assertEqual(Probe.calls, [])
        self.assertFalse(ctx.message.deleted)

    async def test_discord_refusing_the_message_is_reported(self):
        ctx = self.context(author=self.member)
        ctx.send = mock.AsyncMock(side_effect=discord.HTTPException(mock.Mock(status=400, reason="bad"), "Invalid Form Body"))
        await self.tags.use_tag(ctx, await self.make("x", "text"), "")
        self.assertIn("refused", ctx.fails[0])


class CommandBlocksTest(TagBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot._async_setup_hook()  # command errors arrive as events, which need the bot's loop

    async def go(self, content, *, author=None, trusted=None, owner=None, args=""):
        tag = await self.make("x", content, owner=owner or self.admin, trusted=trusted)
        ctx = self.context(author=author or self.member)
        await self.tags.use_tag(ctx, tag, args)
        await asyncio.sleep(0.01)
        return ctx

    def posted(self):
        return [message.content for message in self.channel.sent]

    async def test_every_alias_runs_a_command_as_the_person(self):
        for name in ("command", "cmd", "c"):
            Probe.calls = []
            await self.db.run("DELETE FROM tags")
            await self.go("{%s:probe {args}}" % name, args="hello there")
            self.assertEqual(Probe.calls, [(self.member.id, "hello there")])
        self.assertEqual(self.posted()[-1], "probe hello there")

    async def test_a_command_they_can_not_use_is_refused_with_the_usual_error(self):
        await self.go("{cmd:probeadmin}")
        self.assertEqual(Probe.calls, [])
        self.assertIn("Manage Server", self.posted()[0])

    async def test_override_counts_as_the_bot_manager_role_when_a_moderator_wrote_it(self):
        await self.go("{override}{cmd:probeadmin}")
        self.assertEqual(Probe.calls, [(self.member.id, "admin")])

    async def test_override_does_nothing_from_a_member_s_tag(self):
        await self.go("{=(a):override}{{a}}{cmd:probeadmin}", owner=self.member, trusted=False)
        self.assertEqual(Probe.calls, [])

    async def test_override_never_covers_ban_members(self):
        await self.go("{override}{cmd:probeban}")
        self.assertEqual(Probe.calls, [])

    async def test_cmdadmin_runs_as_the_bot_when_a_moderator_wrote_it(self):
        await self.go("{cmdAdmin:probeadmin}")
        self.assertEqual(Probe.calls, [(self.me.id, "admin")])

    async def test_cmdadmin_uses_the_invoker_s_permissions_from_a_member_s_tag(self):
        await self.go("{=(a):cmdAdmin}{{a}:probeadmin}", owner=self.member, trusted=False)
        self.assertEqual(Probe.calls, [])
        self.assertIn("Manage Server", self.posted()[0])

    async def test_a_member_s_words_can_not_become_a_command_block(self):
        await self.go("{{args}}", args="cmdAdmin:probeadmin")
        self.assertEqual(Probe.calls, [])

    async def test_silent_hides_replies_but_not_errors(self):
        await self.go("{silent}{cmd:probe hi}")
        self.assertEqual(Probe.calls, [(self.member.id, "hi")])
        self.assertEqual(self.posted(), [])
        await self.db.run("DELETE FROM tags")
        self.tags._recent.clear()
        await self.go("{silent}{cmd:probeadmin}")
        self.assertIn("Manage Server", self.posted()[0])

    async def test_they_can_not_reach_tags_or_reaction_roles_or_unknown_commands(self):
        await self.go("{cmd:tag x}{cmd:nothing}")
        self.assertEqual(len(self.posted()), 2)
        self.assertIn("can't run", self.posted()[0])
        self.assertIn("no command", self.posted()[1])

    async def test_a_tag_s_name_is_not_a_command(self):
        await self.make("other", "should not run")
        await self.go("{cmd:other}")
        self.assertIn("no command", self.posted()[0])

    async def test_the_limit_on_command_blocks(self):
        await self.go("{cmd:probe}" * (MAX_COMMANDS + 4))
        self.assertEqual(len(Probe.calls), MAX_COMMANDS)

    async def test_output_comes_first_and_a_redirect_carries_the_command_s_reply(self):
        place = FakeChannel(self.guild, "elsewhere")
        await self.go("moved{cmd:probe}{redirect:elsewhere}")
        self.assertEqual([m.content for m in place.sent], ["moved", "probe"])
        self.assertEqual(self.posted(), [])

    async def test_dm_carries_the_command_s_reply(self):
        await self.go("{dm}{cmd:probe hi}")
        self.assertEqual(self.member.dms[-1]["content"], "probe hi")

    async def test_a_command_s_own_redirect_and_mute_still_apply(self):
        other = FakeChannel(self.guild, "other")
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"probe": {"redirect": other.id}}})
        await self.go("{cmd:probe hi}")
        self.assertEqual([message.content for message in other.sent], ["probe hi"])
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"probe": {"muted": True}}})
        await self.db.run("DELETE FROM tags")
        await self.go("{cmd:probe hi}")
        self.assertEqual(len(Probe.calls), 2)
        self.assertEqual(len(other.sent), 1)
        self.assertEqual(self.posted(), [])

    async def test_a_turned_off_command_stays_off(self):
        await self.bot.settings.save(self.guild.id, "commands", {"overwrites": {"probe": {"enabled": False}}})
        await self.go("{cmdAdmin:probe}")
        self.assertEqual(Probe.calls, [])
        self.assertIn("turned off", self.posted()[0])

    async def test_a_role_trigger_style_run_has_no_message_behind_it(self):
        scope = Scope(guild=self.guild, member=self.member, channel=self.channel)
        result = await run_tag(self.bot, {"content": "plain"}, scope, trusted=True)
        self.assertTrue(result.ran)
        self.assertEqual(self.posted(), ["plain"])


class DashboardTest(TagBase):
    def form(self, **changes):
        return {"name": "support", "message": {"content": "Hello", "embeds": [], "buttons": []}, **changes}

    async def test_create_list_edit_delete(self):
        client = await self.client()
        response = await client.post(self.path("/tags"), json=self.form(restricted=True, role_bl=[str(self.role_mod.id)]))
        self.assertEqual(response.status, 200)
        tag_id = (await response.json())["id"]
        body = await (await client.get(self.path("/tags"))).json()
        self.assertEqual(body["tags"][0]["name"], "support")
        row = await (await client.get(self.path(f"/tags/{tag_id}"))).json()
        self.assertEqual((row["name"], row["message"]["content"], row["uses"], row["restricted"], row["role_bl"], row["owner_id"], row["aliases"], row["share_code"]),
                         ("support", "Hello", 0, True, [str(self.role_mod.id)], str(self.admin.id), [], None))
        self.assertEqual(body["prefixes"], ["!", "?"])
        response = await client.put(self.path(f"/tags/{tag_id}"), json=self.form(name="help", message={"content": "New", "embeds": [{"title": "T", "thumbnail": {"url": "{user(avatar)}"}}]}, restricted=False))
        self.assertEqual(response.status, 200)
        tag = await self.tags.find(self.guild.id, "help")
        self.assertEqual((tag["message"]["content"], tag["restricted"], tag["trusted"]), ("New", False, False))
        self.assertEqual(tag["message"]["embeds"][0]["thumbnail"]["url"], "{user(avatar)}")
        response = await client.delete(self.path(f"/tags/{tag_id}"))
        self.assertEqual(response.status, 200)
        self.assertIsNone(await self.tags.find(self.guild.id, "help"))
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions, ["tag.remove", "tag.edit", "tag.create"])

    async def test_bad_input_names_the_field(self):
        client = await self.client()
        for form, field in [
            (self.form(name="two words"), "name"), (self.form(name=""), "name"), (self.form(name="tag"), "name"), (self.form(message={"content": "", "embeds": []}), "message"),
            (self.form(message={"content": "x" * 25001}), "message"), (self.form(message={"content": "x", "embeds": [{"title": "t", "url": "javascript:x"}]}), "message"),
            (self.form(channel_wl=["nope"]), "channel_wl"), (self.form(nsfw="yes"), "nsfw"), (self.form(bogus=1), "bogus"),
        ]:
            response = await client.post(self.path("/tags"), json=form)
            self.assertEqual(response.status, 400, form)
            self.assertIn(field, (await response.json())["fields"], form)

    async def test_a_name_in_use_is_refused(self):
        client = await self.client()
        await client.post(self.path("/tags"), json=self.form())
        response = await client.post(self.path("/tags"), json=self.form())
        self.assertIn("name", (await response.json())["fields"])
        await self.command("tag_alias", "other", "support")
        response = await client.post(self.path("/tags"), json=self.form(name="other"))
        self.assertEqual(response.status, 400)

    async def test_renaming_to_a_taken_name_is_refused_and_keeping_the_name_is_fine(self):
        client = await self.client()
        first = (await (await client.post(self.path("/tags"), json=self.form())).json())["id"]
        await client.post(self.path("/tags"), json=self.form(name="other"))
        self.assertEqual((await client.put(self.path(f"/tags/{first}"), json=self.form(name="other"))).status, 400)
        self.assertEqual((await client.put(self.path(f"/tags/{first}"), json=self.form())).status, 200)

    async def test_a_tag_in_another_server_can_not_be_reached_by_id(self):
        client = await self.client()
        foreign = await self.tags._insert(999, "foreign", {"content": "x", "embeds": [], "buttons": []}, owner_id=1, trusted=True)
        self.assertEqual((await client.put(self.path(f"/tags/{foreign}"), json=self.form())).status, 404)
        self.assertEqual((await client.delete(self.path(f"/tags/{foreign}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/tags/{foreign}/share"))).status, 404)
        self.assertIsNotNone(await self.tags.find(999, "foreign"))
        self.assertEqual((await (await client.get(self.path("/tags"))).json())["tags"], [])

    async def test_staff_can_not_reach_any_of_it(self):
        client = await self.client(access="staff")
        for method, path in [("get", "/tags"), ("post", "/tags"), ("post", "/tags/import"), ("post", "/tags/preview"), ("get", "/tags/export"), ("delete", "/tags/1")]:
            response = await getattr(client, method)(self.path(path), **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status, 403, path)

    async def test_saved_settings(self):
        client = await self.client()
        self.assertEqual(self.settings("tags"), {"ownership": True, "mod_only": False})
        response = await client.put(self.path("/settings/tags"), json={"ownership": False, "mod_only": True})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("tags"), {"ownership": False, "mod_only": True})

    async def test_preview_renders_without_doing_anything(self):
        ops = self.bot.ops
        ops.member = lambda guild_id, user_id: {"id": user_id, "name": "admin", "display_name": "The Admin", "avatar": "https://cdn.example/a.png", "bot": False, "joined_at": 1000.0, "created_at": 900.0, "role_ids": []}
        ops.describe_guild = lambda guild_id: {"id": guild_id, "name": "Harbor", "icon": None, "member_count": 5, "owner_id": 1, "channels": [{"id": 7, "name": "general", "type": 0, "position": 0}], "roles": [], "emojis": []}
        client = await self.client()
        form = {"args": "there", "message": {"content": "Hi {args} from {user} in {server} #{channel}{delete}{cmd:ban someone}{dm}", "embeds": [{"title": "{user(id)}"}]}}
        body = await (await client.post(self.path("/tags/preview"), json=form)).json()
        self.assertEqual(body["message"]["content"], "Hi there from The Admin in Harbor #general")
        self.assertEqual(body["message"]["embeds"][0]["title"], str(self.admin.id))
        does = " ".join(body["does"])
        self.assertIn("Deletes", does)
        self.assertIn("ban someone", does)
        self.assertIn("DM", does)
        self.assertEqual(self.channel.sent, [])

    async def test_preview_reports_a_runaway_template(self):
        client = await self.client()
        body = await (await client.post(self.path("/tags/preview"), json={"message": {"content": "{=(x):a}" + "{=(x):{x}{x}}" * 60}})).json()
        self.assertIn("too much", body["error"])

    async def test_preview_refuses_a_bad_form(self):
        client = await self.client()
        self.assertEqual((await client.post(self.path("/tags/preview"), json={"message": "nope"})).status, 400)


class ImportExportTest(TagBase):
    # The shape another dashboard hands out, with `embed` as JSON text and dates as text.
    LEGACY = {
        "channel_bl": [], "channel_wl": [], "content": "Check {user}", "created_at": "Mon, 29 Aug 2022 13:03:09 GMT", "embed": "{}", "id": 1, "location_id": 970, "name": "Old",
        "nsfw": False, "owner_id": 176856739293954048, "restricted": True, "role_bl": [], "role_wl": [], "uses": 277,
    }
    LEGACY_EMBED = {
        **LEGACY, "name": "rules", "content": "", "uses": 15,
        "embed": json.dumps({"color": 26367, "title": "Rules", "fields": [{"name": "1", "value": "Be kind", "inline": True}], "image": {"url": "https://cdn.discordapp.com/attachments/1/2/x.png?ex=1&is=2&hm=3"}}),
    }

    async def post(self, client, **form):
        response = await client.post(self.path("/tags/import"), json=form)
        return response.status, await response.json()

    async def test_the_shape_another_dashboard_exports_fills_our_tables(self):
        client = await self.client()
        status, body = await self.post(client, data={"tags": [self.LEGACY, self.LEGACY_EMBED]})
        self.assertEqual((status, body["imported"]), (200, 2))
        tag = await self.tags.find(self.guild.id, "old")
        self.assertEqual((tag["message"]["content"], tag["uses"], tag["owner_id"], tag["restricted"]), ("Check {user}", 277, 176856739293954048, True))
        self.assertEqual(tag["created_at"], 1661778189.0)
        embed = (await self.tags.find(self.guild.id, "rules"))["message"]["embeds"][0]
        self.assertEqual((embed["color"], embed["title"], embed["fields"][0]["value"]), (26367, "Rules", "Be kind"))
        self.assertEqual(embed["image"]["url"], "https://cdn.discordapp.com/attachments/1/2/x.png", "an expiring signature would break the image later")

    async def test_preview_changes_nothing_and_says_what_would_happen(self):
        client = await self.client()
        await self.make("old")
        status, body = await self.post(client, preview=True, data=[self.LEGACY, {"name": "new", "content": "{cmd:ban x}"}, {"name": "two words", "content": "x"}, {"name": "empty"}])
        self.assertEqual((status, body["imported"]), (200, 0))
        rows = {row["name"]: row for row in body["rows"]}
        self.assertEqual(rows["old"]["action"], "skip")
        self.assertEqual((rows["new"]["action"], rows["new"]["commands"]), ("create", ["ban x"]))
        self.assertEqual(rows["two words"]["action"], "skip")
        self.assertIn("spaces", rows["two words"]["reason"])
        self.assertEqual(rows["empty"]["action"], "skip")
        self.assertIsNone(await self.tags.find(self.guild.id, "new"))

    async def test_replace_overwrites_and_keeps_uses(self):
        client = await self.client()
        await self.make("old", "before")
        await self.db.run("UPDATE tags SET uses = 5")
        status, body = await self.post(client, data=[self.LEGACY])
        self.assertEqual(body["imported"], 0)
        status, body = await self.post(client, data=[self.LEGACY], replace=True)
        self.assertEqual(body["imported"], 1)
        tag = await self.tags.find(self.guild.id, "old")
        self.assertEqual((tag["message"]["content"], tag["uses"]), ("Check {user}", 5))

    async def test_a_name_twice_in_a_file_only_lands_once(self):
        client = await self.client()
        status, body = await self.post(client, data=[{"name": "x", "content": "1"}, {"name": "X", "content": "2"}])
        self.assertEqual((status, body["imported"]), (200, 1))
        self.assertEqual((await self.tags.find(self.guild.id, "x"))["message"]["content"], "1")

    async def test_export_then_import_is_the_same_tag(self):
        client = await self.client()
        tag = await self.make("x", "{user}", embeds=[{"title": "T"}], nsfw=True, restricted=True, uses=4)
        await self.command("tag_alias", "y", "x")
        exported = await (await client.get(self.path("/tags/export"))).json()
        self.assertEqual(exported["kind"], "tags")
        self.assertEqual(exported["tags"][0]["aliases"], ["y"])
        await self.db.run("DELETE FROM tags")
        await self.db.run("DELETE FROM tag_aliases")
        status, body = await self.post(client, data=json.dumps(exported))
        self.assertEqual((status, body["imported"]), (200, 1))
        again = await self.tags.find(self.guild.id, "y")
        self.assertEqual((again["name"], again["message"], again["nsfw"], again["restricted"], again["uses"]), ("x", tag["message"], True, True, 4))

    async def test_export_can_pick_one_tag(self):
        client = await self.client()
        await self.make("a")
        chosen = await self.make("b")
        exported = await (await client.get(self.path(f"/tags/export?ids={chosen['id']}"))).json()
        self.assertEqual([row["name"] for row in exported["tags"]], ["b"])

    async def test_channels_and_roles_from_another_server_are_dropped(self):
        client = await self.client()
        known_role, known_channel = self.role_mod.id, self.channel.id
        self.bot.ops.describe_guild = lambda guild_id: {"channels": [{"id": known_channel}], "roles": [{"id": known_role}]}
        await self.post(client, data=[{"name": "x", "content": "y", "channel_wl": [known_channel, 111111111111111111], "role_bl": [known_role, 222222222222222222], "role_wl": "junk"}])
        tag = await self.tags.find(self.guild.id, "x")
        self.assertEqual((tag["channel_wl"], tag["role_bl"], tag["role_wl"]), ([known_channel], [known_role], []))

    async def test_bad_files(self):
        client = await self.client()
        for data, field in [("{nope", "data"), (5, "data"), ({"other": 1}, None), ([{"name": "x", "embed": "{nope", "content": ""}], None)]:
            status, body = await self.post(client, data=data)
            if field:
                self.assertEqual(status, 400, data)
                self.assertIn(field, body["fields"])
            else:
                self.assertEqual((status, body["imported"], body["rows"][0]["action"]), (200, 0, "skip"))
        self.assertEqual((await client.post(self.path("/tags/import"), json=[1, 2])).status, 400)

    async def test_too_much_at_once(self):
        client = await self.client()
        status, body = await self.post(client, data=[{"name": str(i), "content": "x"} for i in range(2001)])
        self.assertEqual(status, 400)

    async def test_a_share_code_brings_the_tag_to_another_server_without_its_lists(self):
        client = await self.client()
        await self.make("x", "shared", role_wl=[self.role_mod.id], uses=9)
        code = (await (await client.post(self.path(f"/tags/{(await self.tags.find(self.guild.id, 'x'))['id']}/share"))).json())["code"]
        other = FakeGuild("Other")
        self.bot.ops.access_for = lambda guild_id, user_id: "full"
        path = f"/api/g/{other.id}/tags/import"
        response = await client.post(path, json={"code": code})
        self.assertEqual((await response.json())["imported"], 1)
        copy = await self.tags.find(other.id, "x")
        self.assertEqual((copy["message"]["content"], copy["role_wl"], copy["uses"], copy["owner_id"]), ("shared", [], 0, self.admin.id))
        response = await client.delete(self.path(f"/tags/{(await self.tags.find(self.guild.id, 'x'))['id']}/share"))
        self.assertEqual(response.status, 200)
        response = await client.post(path, json={"code": code})
        self.assertEqual(response.status, 400)
        self.assertIn("code", (await response.json())["fields"])

    def test_imported_tag_tolerates_odd_values(self):
        tag = imported_tag({"name": "x", "content": "y", "uses": "many", "nsfw": "yes", "owner_id": "nope", "created_at": "whenever", "aliases": ["ok", "two words"], "channel_wl": None})
        self.assertEqual((tag["uses"], tag["nsfw"], tag["owner_id"], tag["created_at"], tag["aliases"], tag["channel_wl"]), (0, False, None, None, ["ok"], []))
        tag = imported_tag({"name": "x", "content": "y", "uses": 1e300, "owner_id": 5, "created_at": 1e300, "aliases": 5, "channel_wl": [5, "x", None, 1.5, [], 99999999999999999999, 100000000000000001]})
        self.assertEqual((tag["uses"], tag["owner_id"], tag["created_at"], tag["aliases"], tag["channel_wl"]), (0, None, None, [], [100000000000000001]))

    def test_imported_tag_refuses_a_malformed_message_with_a_sentence(self):
        for item in ({"name": "x", "embeds": [5]}, {"name": "x", "embeds": 5}, {"name": "x", "content": 5}, {"name": "x", "content": "y", "buttons": 5}, {"name": "x", "embed": "[1]"}, 5):
            with self.assertRaises(Invalid):
                imported_tag(item)

    async def test_json_nested_too_deeply_is_only_a_bad_file(self):
        client = await self.client()
        status, body = await self.post(client, data="[" * 200000)
        self.assertEqual(status, 400)
        self.assertIn("data", body["fields"])


class RealLibraryTest(GatewayTest):
    """Through discord.py itself: the prefix path, slash commands and command blocks."""
    modules = [Config, Tags]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.guild.default_role._permissions = discord.Permissions(view_channel=True, send_messages=True).value  # /tag get asks whether the person can post

    async def make(self, name="support", content="Hello {user}", **flags):
        cog = next(m for m in self.bot.modules if isinstance(m, Tags))
        await cog._insert(self.guild_id, name, {"content": content, "embeds": flags.pop("embeds", []), "buttons": []}, owner_id=self.owner_id, trusted=True, **flags)
        return cog

    def said(self):
        return [message["content"] for message in self.net.posted]

    async def test_running_a_tag_by_prefix(self):
        cog = await self.make()
        self.dispatch_message(user_id=self.member_id, content="!support and more")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["Hello member"])
        self.assertEqual((await cog.find(self.guild_id, "support"))["uses"], 1)
        self.assertEqual(await self.db.scalar("SELECT uses FROM command_uses WHERE command = 'tag'"), 1)

    async def test_case_and_aliases_and_a_mention_prefix(self):
        cog = await self.make()
        await self.db.run("INSERT INTO tag_aliases (guild_id, name, tag_id, created_at) VALUES (?, 'assist', 1, 0)", (self.guild_id,))
        self.dispatch_message(user_id=self.member_id, content="!SUPPORT")
        await self.settle()
        cog._recent.clear()
        self.dispatch_message(user_id=self.member_id, content="!assist")
        await self.settle()
        cog._recent.clear()
        self.dispatch_message(user_id=self.member_id, content=f"<@{self.bot_id}> support")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["Hello member"] * 3)

    async def test_chat_that_only_looks_like_a_prefix_stays_quiet(self):
        await self.make()
        self.dispatch_message(user_id=self.member_id, content="!whatever this is")
        self.dispatch_message(user_id=self.member_id, content="support")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), [])

    async def test_bots_can_not_run_tags(self):
        await self.make()
        self.dispatch_message(user_id=self.bot_id, content="!support")
        await self.settle()
        self.assertEqual(self.said(), [])

    async def test_a_turned_off_tag_command_stops_tags_quietly(self):
        await self.make()
        await self.bot.settings.save(self.guild_id, "commands", {"overwrites": {"tag": {"enabled": False}}})
        self.dispatch_message(user_id=self.member_id, content="!support")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), [])

    def slash(self, name, value, *, user_id=None):
        data = {"id": str(new_id()), "name": "tag", "type": 1, "options": [{"name": "get", "type": 1, "options": [{"name": "name", "type": 3, "value": name}, {"name": "args", "type": 3, "value": value}]}]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.member_id))

    async def complete(self, subcommand, typed):
        data = {"id": str(new_id()), "name": "tag", "type": 1, "options": [{"name": subcommand, "type": 1, "options": [{"name": "name", "type": 3, "value": typed, "focused": True}]}]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(4, data, user_id=self.member_id))
        await self.settle()
        self.assert_no_errors()
        return [choice["value"] for method, path, body in self.net.calls if "callback" in path and body.get("type") == 8 for choice in body["data"]["choices"]]

    async def test_autocomplete_through_the_real_tree(self):
        await self.make("support")
        await self.make("supplies")
        await self.make("other")
        self.assertEqual(await self.complete("get", "sup"), ["supplies", "support"])
        self.net.calls.clear()
        self.assertEqual(await self.complete("edit", "oth"), ["other"])

    async def test_tag_get_answers_the_interaction(self):
        await self.make()
        self.slash("support", "x")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["Hello member"])

    async def test_a_slash_tag_with_no_output_is_still_answered(self):
        await self.make("quiet", "{=(a):1}")
        self.slash("quiet", "x")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["Done."])

    async def test_a_slash_tag_with_a_redirect_is_answered_and_posts_elsewhere(self):
        await self.make("moved", "elsewhere{redirect:logs}")
        self.slash("moved", "x")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(sorted(self.said()), ["Done.", "elsewhere"])
        self.assertIn(("POST", "/channels/{channel_id}/messages"), [(method, path) for method, path, _ in self.net.calls])

    async def test_a_command_block_runs_through_the_real_library(self):
        await self.make("alias", "{cmd:prefix list}")
        self.dispatch_message(user_id=self.member_id, content="!alias")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["Prefixes: `!`, `?`"])

    async def test_a_command_block_from_slash(self):
        await self.make("alias", "said{cmd:prefix list}")
        self.slash("alias", "x")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.said(), ["said", "Prefixes: `!`, `?`"])

    async def test_a_command_block_s_permission_refusal(self):
        await self.make("alias", "{cmd:prefix add ?}")
        self.dispatch_message(user_id=self.member_id, content="!alias")
        await self.settle()
        self.assert_no_errors()
        self.assertIn("Manage Server", self.said()[0])
        self.assertEqual(self.bot.settings.get(self.guild_id, "core")["prefixes"], ["!", "?"])

    async def test_cmdadmin_acts_as_the_bot(self):
        await self.make("alias", "{cmdAdmin:prefix add ?}")
        self.dispatch_message(user_id=self.member_id, content="!alias")
        await self.settle()
        self.assert_no_errors()
        self.assertEqual(self.bot.settings.get(self.guild_id, "core")["prefixes"], ["!", "?"])

    async def test_delete_react_and_dm_reach_discord(self):
        await self.make("all", "secret{delete}{dm}{reactu: \N{THUMBS UP SIGN}}{react:\N{THUMBS UP SIGN}}")
        self.dispatch_message(user_id=self.member_id, content="!all")
        await self.settle()
        self.assert_no_errors()
        paths = [(method, path) for method, path, _ in self.net.calls]
        self.assertIn(("DELETE", "/channels/{channel_id}/messages/{message_id}"), paths)
        self.assertIn(("POST", "/users/@me/channels"), paths)
        self.assertEqual(len([p for p in paths if "/reactions/" in p[1]]), 1, "the reaction on the output, since the invocation is deleted")
        self.assertEqual(self.said(), ["secret"])

    async def test_an_embed_goes_out_as_discord_wants_it(self):
        await self.make("rich", "", embeds=[{"title": "{user}", "color": 26367, "fields": [{"name": "1", "value": "{server}", "inline": True}], "thumbnail": {"url": "{user(avatar)}"}}])
        self.dispatch_message(user_id=self.member_id, content="!rich")
        await self.settle()
        self.assert_no_errors()
        embed = self.net.posted[0]["embeds"][0]
        self.assertEqual((embed["title"], embed["fields"][0]["value"], embed["color"]), ("member", "Test Guild", 26367))
        self.assertTrue(embed["thumbnail"]["url"].startswith("https://"))

    async def test_real_member_properties(self):
        role_id = int(self.role_member["id"])
        await self.make("who", "{user(id)}|{user(name)}|{user(proper)}|{user(mention)}|{user(position)}|{user(color)}|{user(roleids)}|{user(created_at)}|{user(joined_at)}|{user(avatar)}")
        self.dispatch_message(user_id=self.member_id, content="!who")
        await self.settle()
        self.assert_no_errors()
        parts = self.said()[0].split("|")
        self.assertEqual(parts[:6], [str(self.member_id), "member", "member", f"<@{self.member_id}>", "2", "#000000"])
        self.assertEqual(parts[6], f"{self.guild_id} {role_id}")
        self.assertRegex(parts[7], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(parts[8], "2025-01-01 00:00:00")
        self.assertTrue(parts[9].startswith("https://"))

    async def test_real_server_and_channel_properties(self):
        await self.make("where", "{server}|{server(id)}|{server(owner)}|{server(members)}|{server(bots)}|{server(humans)}|{server(roles)}|{server(channels)}|{server(icon)}|{channel}|{channel(id)}|{channel(topic)}|{channel(slowmode)}|{channel(position)}|{channel(mention)}|{server(random)}|{server(randomonline)}")
        self.dispatch_message(user_id=self.member_id, content="!where")
        await self.settle()
        self.assert_no_errors()
        parts = self.said()[0].split("|")
        self.assertEqual(parts[:9], ["Test Guild", str(self.guild_id), "owner", "5", "1", "4", "6", "5", ""])
        self.assertEqual(parts[9:15], ["general", self.text["id"], "", "0", "1", f"<#{self.text['id']}>"])
        self.assertIn(parts[15], {"Captain Guild", "owner", "admin", "mod", "member"})
        self.assertIn(parts[16], {"Captain Guild", "owner", "admin", "mod", "member"})


class SlashTreeTest(BotTest):
    modules = [Tags]

    async def test_descriptions_fit(self):
        for command in self.cog(Tags).tag.walk_commands():
            self.assertLessEqual(len(command.description), 100, command.name)

    async def test_the_group_is_on_the_tree(self):
        self.assertIsInstance(self.bot.tree.get_command("tag"), app_commands.Group)
