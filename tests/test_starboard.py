import asyncio
from types import SimpleNamespace

import discord

from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.modules.starboard import Starboard, _emoji_value
from captainguild.core.validate import Invalid
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage, FakePermissions, FakeRole, _FakeResponse, new_id
from tests.test_access_limits import SiteCase

STAR = "\N{WHITE MEDIUM STAR}"


class FakeAttachment:
    def __init__(self, filename, content_type="image/png"):
        self.id = new_id()
        self.filename = filename
        self.url = f"https://cdn.example/{filename}"
        self.content_type = content_type


class StarboardCase(BotTest):
    modules = [Starboard]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.cog_ = self.cog(Starboard)
        self.board = FakeChannel(self.guild, "starboard")
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": self.board.id, "threshold": 2})
        self.alice = FakeMember(self.guild, "alice")
        self.bob = FakeMember(self.guild, "bob")
        self.carol = FakeMember(self.guild, "carol")
        self.message = self.say(self.member, "A very good post")

    def say(self, author, content="hello", channel=None, **kwargs):
        channel = channel or self.channel
        message = FakeMessage(channel, author, content, **kwargs)
        channel.sent.append(message)
        return message

    def event(self, who, message=None, emoji=STAR, **changes):
        message = message or self.message
        data = {
            "guild_id": self.guild.id, "channel_id": message.channel.id, "message_id": message.id, "user_id": who.id, "member": who,
            "message_author_id": message.author.id, "emoji": emoji if isinstance(emoji, discord.PartialEmoji) else discord.PartialEmoji(name=emoji),
        }
        return SimpleNamespace(**{**data, **changes})

    async def star(self, *members, message=None, emoji=STAR):
        for member in members:
            await self.cog_.on_raw_reaction_add(self.event(member, message, emoji))

    async def unstar(self, *members, message=None):
        for member in members:
            await self.cog_.on_raw_reaction_remove(self.event(member, message, member=None))

    async def entry(self, message=None):
        return await self.db.one("SELECT * FROM starboard_entries WHERE message_id = ?", ((message or self.message).id,))


class CountingTest(StarboardCase):
    async def test_a_message_goes_up_at_the_limit_with_its_text_and_author(self):
        await self.star(self.alice)
        self.assertEqual(self.board.sent, [])
        await self.star(self.bob)
        post = self.board.sent[0]
        self.assertEqual(post.content, f"{STAR} **2** <#{self.channel.id}>")
        embed = post.embeds[0]
        self.assertEqual(embed.description, "A very good post")
        self.assertEqual(embed.author.name, "member")
        self.assertIn(self.message.jump_url, embed.fields[-1].value)
        self.assertEqual(embed.footer.text, f"ID: {self.message.id}")
        self.assertFalse(post.kwargs["allowed_mentions"].users)

    async def test_more_stars_only_change_the_count(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]
        await self.star(self.carol)
        self.assertEqual(len(self.board.sent), 1)
        self.assertEqual(post.content, f"{STAR} **3** <#{self.channel.id}>")

    async def test_falling_below_the_limit_takes_it_down_and_it_can_return(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]
        await self.unstar(self.bob)
        self.assertTrue(post.deleted)
        entry = await self.entry()
        self.assertIsNone(entry["star_message_id"])
        self.assertEqual(entry["removed"], 0)
        await self.cog_.on_raw_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.board.id, message_id=post.id))
        await self.star(self.bob)
        self.assertEqual(len(self.board.sent), 1)
        self.assertIsNot(self.board.sent[0], post)

    async def test_starring_twice_counts_once(self):
        await self.star(self.alice, self.alice)
        self.assertEqual((await self.entry())["stars"], 1)
        self.assertEqual(self.board.sent, [])

    async def test_your_own_message_doesnt_count_unless_allowed(self):
        await self.star(self.member, self.alice)
        self.assertEqual(self.board.sent, [])
        self.assertEqual((await self.entry())["stars"], 1)
        await self.bot.settings.save(self.guild.id, "starboard", {"star_self": True})
        await self.star(self.member)
        self.assertEqual(len(self.board.sent), 1)

    async def test_the_author_is_looked_up_when_the_event_doesnt_say(self):
        await self.cog_.on_raw_reaction_add(self.event(self.member, message_author_id=None))
        self.assertIsNone(await self.entry())
        await self.cog_.on_raw_reaction_add(self.event(self.alice, message_author_id=None))
        self.assertEqual((await self.entry())["stars"], 1)

    async def test_bots_dont_count(self):
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.star(robot, self.alice)
        self.assertEqual((await self.entry())["stars"], 1)

    async def test_other_emoji_dont_count(self):
        await self.star(self.alice, self.bob, emoji="\N{THUMBS UP SIGN}")
        self.assertIsNone(await self.entry())

    async def test_a_custom_emoji_counts_by_id_even_after_a_rename(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"emoji": "<:party:123456789012345678>"})
        renamed = discord.PartialEmoji(name="celebrate", id=123456789012345678)
        await self.cog_.on_raw_reaction_add(self.event(self.alice, emoji=renamed))
        await self.cog_.on_raw_reaction_add(self.event(self.bob, emoji=renamed))
        self.assertEqual(self.board.sent[0].content, f"<:party:123456789012345678> **2** <#{self.channel.id}>")
        await self.star(self.carol)
        self.assertEqual((await self.entry())["stars"], 2)

    async def test_a_unicode_emoji_matches_with_or_without_its_variation_mark(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"emoji": "\N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}"})
        await self.star(self.alice, emoji="\N{HEAVY BLACK HEART}")
        await self.star(self.bob, emoji="\N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}")
        self.assertEqual(len(self.board.sent), 1)

    async def test_a_lock_freezes_everything(self):
        await self.star(self.alice, self.bob)
        await self.bot.settings.save(self.guild.id, "starboard", {"locked": True})
        await self.star(self.carol)
        await self.unstar(self.alice)
        self.assertEqual((await self.entry())["stars"], 2)
        self.assertFalse(self.board.sent[0].deleted)

    async def test_a_lock_holds_when_reactions_are_cleared_too(self):
        await self.star(self.alice, self.bob)
        await self.bot.settings.save(self.guild.id, "starboard", {"locked": True})
        await self.cog_.on_raw_reaction_clear(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.message.id))
        self.assertEqual((await self.entry())["stars"], 2)
        self.assertFalse(self.board.sent[0].deleted)

    async def test_ignored_channels_and_their_threads_dont_count(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"blacklist": [self.channel.id]})
        await self.star(self.alice, self.bob)
        self.assertEqual(self.board.sent, [])
        thread = FakeChannel(self.guild, "a-thread")
        thread.parent_id = self.channel.id
        await self.star(self.alice, self.bob, message=self.say(self.member, channel=thread))
        self.assertEqual(self.board.sent, [])

    async def test_no_channel_means_nothing_happens(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": None})
        await self.star(self.alice, self.bob)
        self.assertIsNone(await self.entry())

    async def test_stars_at_the_same_moment_post_one_copy(self):
        await asyncio.gather(*(self.cog_.on_raw_reaction_add(self.event(person)) for person in (self.alice, self.bob, self.carol)))
        self.assertEqual(len(self.board.sent), 1)
        self.assertEqual((await self.entry())["stars"], 3)

    async def test_a_channel_setting_can_only_reach_this_server(self):
        other = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(other)
        elsewhere = FakeChannel(other, "elsewhere")
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": elsewhere.id})
        await self.star(self.alice, self.bob)
        self.assertEqual(elsewhere.sent, [])

    async def test_a_message_gone_before_it_was_read_is_dropped(self):
        await self.star(self.alice)
        self.channel.sent.remove(self.message)
        await self.star(self.bob)
        self.assertIsNone(await self.entry())
        self.assertEqual(self.board.sent, [])


class BoardPostTest(StarboardCase):
    async def test_a_star_on_the_board_post_counts_for_the_original_once(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]
        await self.star(self.carol, message=post)
        self.assertEqual(post.content, f"{STAR} **3** <#{self.channel.id}>")
        await self.star(self.alice, message=post)
        self.assertEqual((await self.entry())["stars"], 3)
        await self.unstar(self.alice, message=post)
        self.assertEqual((await self.entry())["stars"], 3)
        await self.unstar(self.alice)
        self.assertEqual((await self.entry())["stars"], 2)

    async def test_the_author_cant_star_their_own_board_post(self):
        await self.star(self.alice, self.bob)
        await self.star(self.member, message=self.board.sent[0])
        self.assertEqual((await self.entry())["stars"], 2)

    async def test_other_messages_in_the_board_channel_dont_count(self):
        stray = self.say(self.member, channel=self.board)
        await self.star(self.alice, self.bob, message=stray)
        self.assertIsNone(await self.entry(stray))

    async def test_a_post_a_moderator_deleted_stays_gone(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]
        self.board.sent.remove(post)
        await self.cog_.on_raw_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.board.id, message_id=post.id))
        self.assertEqual((await self.entry())["removed"], 1)
        await self.star(self.carol)
        self.assertEqual(self.board.sent, [])

    async def test_a_post_deleted_while_the_bot_was_away_stays_gone_too(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]

        async def missing(**kwargs):
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "Unknown Message")

        self.board.get_partial_message = lambda message_id: SimpleNamespace(edit=missing)
        await self.star(self.carol)
        self.assertEqual((await self.entry())["removed"], 1)
        self.assertEqual(self.board.sent, [post])

    async def test_deleting_the_original_takes_the_post_with_it(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]
        await self.cog_.on_raw_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.message.id))
        self.assertTrue(post.deleted)
        self.assertIsNone(await self.entry())
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM starboard_stars"), 0)

    async def test_bulk_deletes_cover_both_sides(self):
        second = self.say(self.member, "Another")
        await self.star(self.alice, self.bob)
        await self.star(self.alice, self.bob, message=second)
        first_post, second_post = self.board.sent
        await self.cog_.on_raw_bulk_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_ids={self.message.id, second_post.id}))
        self.assertTrue(first_post.deleted)
        self.assertEqual((await self.entry(second))["removed"], 1)

    async def test_clearing_reactions_takes_the_post_down(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]
        await self.cog_.on_raw_reaction_clear(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.message.id))
        self.assertTrue(post.deleted)
        self.assertEqual((await self.entry())["stars"], 0)

    async def test_clearing_one_emoji_only_counts_for_the_star(self):
        await self.star(self.alice, self.bob)
        post = self.board.sent[0]
        await self.cog_.on_raw_reaction_clear_emoji(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.message.id, emoji=discord.PartialEmoji(name="\N{THUMBS UP SIGN}")))
        self.assertFalse(post.deleted)
        await self.cog_.on_raw_reaction_clear_emoji(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.message.id, emoji=discord.PartialEmoji(name=STAR)))
        self.assertTrue(post.deleted)

    async def test_an_edit_to_the_original_reaches_the_post(self):
        await self.star(self.alice, self.bob)
        self.message.content = "Edited text"
        await self.cog_.on_raw_message_edit(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.message.id))
        self.assertEqual(self.board.sent[0].embeds[0].description, "Edited text")
        self.assertEqual((await self.entry())["preview"], "Edited text")

    async def test_deleting_the_board_channel_clears_the_setting(self):
        await self.star(self.alice, self.bob)
        await self.cog_.on_guild_channel_delete(self.board)
        self.assertIsNone(self.settings("starboard")["channel_id"])
        self.assertIsNone((await self.entry())["star_message_id"])

    async def test_autostar_adds_the_servers_emoji(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"autostar": True, "emoji": "\N{THUMBS UP SIGN}"})
        await self.star(self.alice, self.bob, emoji="\N{THUMBS UP SIGN}")
        self.assertEqual(self.board.sent[0].added_reactions, ["\N{THUMBS UP SIGN}"])

    async def test_a_refused_post_is_no_crash_and_comes_with_the_next_star(self):
        real = self.board.send

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Access")

        self.board.send = refuse
        await self.star(self.alice, self.bob)
        self.board.send = real
        await self.star(self.carol)
        self.assertEqual(len(self.board.sent), 1)


class ContentTest(StarboardCase):
    async def post_for(self, message):
        await self.star(self.alice, self.bob, message=message)
        return self.board.sent[-1]

    async def test_the_first_image_shows_and_other_files_are_links(self):
        message = self.say(self.member, "Look", attachments=[FakeAttachment("a.png"), FakeAttachment("b.png"), FakeAttachment("c.zip", "application/zip")])
        post = await self.post_for(message)
        embed = post.embeds[0]
        self.assertEqual(embed.image.url, "https://cdn.example/a.png")
        self.assertIn("b.png", embed.fields[0].value)
        self.assertIn("c.zip", embed.fields[0].value)

    async def test_a_spoiler_image_isnt_shown(self):
        post = await self.post_for(self.say(self.member, "Look", attachments=[FakeAttachment("SPOILER_a.png")]))
        self.assertIsNone(post.embeds[0].image.url)
        self.assertIn("SPOILER", post.embeds[0].fields[0].value)

    async def test_nsfw_images_stay_hidden_until_allowed(self):
        lewd = FakeChannel(self.guild, "lewd", nsfw=True)
        post = await self.post_for(self.say(self.member, "Look", channel=lewd, attachments=[FakeAttachment("a.png")]))
        self.assertIsNone(post.embeds[0].image.url)
        self.assertNotIn("a.png", post.embeds[0].fields[0].value)
        await self.bot.settings.save(self.guild.id, "starboard", {"nsfw": True})
        post = await self.post_for(self.say(self.member, "Look", channel=lewd, attachments=[FakeAttachment("b.png")]))
        self.assertEqual(post.embeds[0].image.url, "https://cdn.example/b.png")

    async def test_an_nsfw_board_shows_them_anyway(self):
        lewd = FakeChannel(self.guild, "lewd", nsfw=True)
        self.board.nsfw = True
        post = await self.post_for(self.say(self.member, "Look", channel=lewd, attachments=[FakeAttachment("a.png")]))
        self.assertEqual(post.embeds[0].image.url, "https://cdn.example/a.png")

    async def test_an_embed_only_message_borrows_its_text_and_image(self):
        inner = discord.Embed(title="T", description="D", type="rich")
        inner.set_image(url="https://example.com/i.png")
        post = await self.post_for(self.say(self.member, "", embeds=[inner]))
        self.assertEqual(post.embeds[0].description, "T\nD")
        self.assertEqual(post.embeds[0].image.url, "https://example.com/i.png")

    async def test_the_jump_link_can_be_off(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"show_jump": False})
        post = await self.post_for(self.message)
        self.assertEqual(post.embeds[0].fields, [])


class StatsAndCommandTest(StarboardCase):
    async def build(self):
        self.second = self.say(self.bob, "Second")
        await self.star(self.alice, self.bob, self.carol)
        await self.star(self.alice, self.carol, message=self.second)

    async def test_the_stats_endpoint(self):
        await self.build()
        client = await self.client()
        data = await (await client.get(self.path("/starboard/stats"))).json()
        self.assertEqual((data["totals"]["posts"], data["totals"]["stars"], data["totals"]["givers"], data["totals"]["given"]), (2, 5, 3, 5))
        self.assertEqual([p["stars"] for p in data["posts"]], [3, 2])
        self.assertEqual(data["posts"][0]["preview"], "A very good post")
        self.assertEqual(data["posts"][0]["author_name"], "member")
        self.assertEqual(data["authors"][0]["name"], "member")
        self.assertEqual(data["givers"][0]["given"], 2)

    async def test_stats_stay_in_this_server(self):
        await self.build()
        await self.db.run("UPDATE starboard_entries SET guild_id = 999")
        await self.db.run("UPDATE starboard_stars SET guild_id = 999")
        client = await self.client()
        data = await (await client.get(self.path("/starboard/stats"))).json()
        self.assertEqual(data["totals"]["posts"], 0)
        self.assertEqual(data["posts"], [])

    async def test_create_makes_a_read_only_channel_once(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": None})
        client = await self.client()
        response = await client.post(self.path("/starboard/create"), json={"name": "best-of"})
        self.assertEqual(response.status, 200)
        made = next(c for c in self.guild.channels if c.name == "best-of")
        self.assertEqual(self.settings("starboard")["channel_id"], made.id)
        self.assertIs(made.overwrites[self.guild.default_role].send_messages, False)
        self.assertIs(made.overwrites[self.guild.me].send_messages, True)
        self.assertEqual((await client.post(self.path("/starboard/create"), json={"name": "again"})).status, 409)
        self.assertEqual((await client.post(self.path("/starboard/create"), json={"name": ""})).status, 400)

    async def test_settings_check_what_they_get(self):
        client = await self.client()
        self.assertEqual((await client.put(self.path("/settings/starboard"), json={"threshold": 0})).status, 400)
        self.assertEqual((await client.put(self.path("/settings/starboard"), json={"threshold": 101})).status, 400)
        self.assertEqual((await client.put(self.path("/settings/starboard"), json={"emoji": "not an emoji"})).status, 400)
        self.assertEqual((await client.put(self.path("/settings/starboard"), json={"emoji": "5"})).status, 400)
        self.assertEqual((await client.put(self.path("/settings/starboard"), json={"emoji": "5\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}"})).status, 200)
        self.assertEqual((await client.put(self.path("/settings/starboard"), json={"emoji": "<:party:123456789012345678>"})).status, 200)
        self.assertEqual((await client.put(self.path("/settings/starboard"), json={"emoji": None})).status, 200)

    async def test_staff_are_refused(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/starboard/stats"))).status, 403)
        self.assertEqual((await client.post(self.path("/starboard/create"), json={"name": "x"})).status, 403)

    async def test_setup_uses_a_channel_or_finds_one_by_name_or_makes_one(self):
        ctx = self.context()
        target = FakeChannel(self.guild, "stars")
        await self.cog_.starboard_setup.callback(self.cog_, ctx, target)
        self.assertEqual(self.settings("starboard")["channel_id"], target.id)
        await self.cog_.starboard_setup.callback(self.cog_, ctx)
        self.assertEqual(self.settings("starboard")["channel_id"], self.board.id)
        await self.cog_.starboard_setup.callback(self.cog_, ctx, name="new:brand-new")
        made = next(c for c in self.guild.channels if c.name == "brand-new")
        self.assertEqual(self.settings("starboard")["channel_id"], made.id)

    async def test_setting_commands(self):
        ctx = self.context()
        await self.cog_.starboard_limit.callback(self.cog_, ctx, 5)
        self.assertEqual(self.settings("starboard")["threshold"], 5)
        await self.cog_.starboard_limit.callback(self.cog_, ctx, 0)
        self.assertTrue(ctx.fails)
        for command, field in ((self.cog_.starboard_nsfw, "nsfw"), (self.cog_.starboard_self, "star_self"), (self.cog_.starboard_autostar, "autostar"), (self.cog_.starboard_lock, "locked"), (self.cog_.starboard_jump, "show_jump")):
            before = self.settings("starboard")[field]
            await command.callback(self.cog_, ctx)
            self.assertEqual(self.settings("starboard")[field], not before, field)
        await self.cog_.starboard.callback(self.cog_, ctx)
        self.assertIn("Stars needed: 5", ctx.sent[-1]["content"])

    async def test_blacklist_takes_mentions_names_and_ids(self):
        other = FakeChannel(self.guild, "other")
        ctx = self.context()
        await self.cog_.starboard_blacklist.callback(self.cog_, ctx, channels=f"{self.channel.mention} other")
        self.assertEqual(self.settings("starboard")["blacklist"], [self.channel.id, other.id])
        await self.cog_.starboard_unblacklist.callback(self.cog_, ctx, channels=str(other.id))
        self.assertEqual(self.settings("starboard")["blacklist"], [self.channel.id])
        await self.cog_.starboard_blacklist.callback(self.cog_, ctx, channels="nothing-like-that")
        self.assertTrue(ctx.fails)

    async def test_emoji_set_and_reset(self):
        ctx = self.context()
        await self.cog_.starboard_emoji.callback(self.cog_, ctx, "set", "\N{THUMBS UP SIGN}")
        self.assertEqual(self.settings("starboard")["emoji"], "\N{THUMBS UP SIGN}")
        await self.cog_.starboard_emoji.callback(self.cog_, ctx, "set")
        await self.cog_.starboard_emoji.callback(self.cog_, ctx, "set", "words")
        self.assertEqual(len(ctx.fails), 2)
        await self.cog_.starboard_emoji.callback(self.cog_, ctx, "reset")
        self.assertIsNone(self.settings("starboard")["emoji"])

    async def test_remove_stops_using_the_channel(self):
        ctx = self.context()
        await self.cog_.starboard_remove.callback(self.cog_, ctx)
        self.assertIsNone(self.settings("starboard")["channel_id"])
        await self.cog_.starboard_remove.callback(self.cog_, ctx)
        self.assertTrue(ctx.fails)

    async def test_show_finds_a_post_by_either_id_or_a_link(self):
        await self.build()
        ctx = self.context()
        post = self.board.sent[0]
        for reference in (str(self.message.id), str(post.id), post.jump_url):
            await self.cog_.starboard_show.callback(self.cog_, ctx, reference)
            self.assertEqual(ctx.sent[-1]["content"], post.content)
            self.assertFalse(ctx.sent[-1]["allowed_mentions"].users)
        await self.cog_.starboard_show.callback(self.cog_, ctx, "123456789012345678")
        await self.cog_.starboard_show.callback(self.cog_, ctx, "words")
        await self.cog_.starboard_show.callback(self.cog_, ctx, "99999999999999999999")
        self.assertEqual(len(ctx.fails), 3)

    async def test_show_wont_read_a_board_to_someone_who_cant_open_it(self):
        await self.build()
        self.board.permissions_for = lambda member: FakePermissions("view_channel")
        ctx = self.context(author=self.member)
        await self.cog_.starboard_show.callback(self.cog_, ctx, str(self.message.id))
        self.assertTrue(ctx.fails)
        self.assertEqual(ctx.sent, [])

    async def test_random_and_the_numbers_commands(self):
        ctx = self.context()
        await self.cog_.starboard_random.callback(self.cog_, ctx)
        self.assertIn("empty", ctx.fails[0])
        await self.build()
        await self.cog_.starboard_random.callback(self.cog_, ctx)
        self.assertIn(STAR, ctx.sent[-1]["content"])
        await self.cog_.starboard_server.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[-1]["embed"].fields[0].value, "2")
        await self.cog_.starboard_stats.callback(self.cog_, ctx)
        self.assertEqual(ctx.sent[-1]["embed"].fields[0].value, "2")
        await self.cog_.starboard_stats.callback(self.cog_, ctx, self.member)
        embed = ctx.sent[-1]["embed"]
        self.assertEqual((embed.fields[0].value, embed.fields[1].value), ("1", "3"))
        await self.cog_.starboard_stats.callback(self.cog_, ctx, self.alice)
        self.assertEqual(ctx.sent[-1]["embed"].fields[2].value, "2")

    async def test_who_may_use_what(self):
        for command in (self.cog_.starboard, self.cog_.starboard_setup, self.cog_.starboard_limit, self.cog_.starboard_nsfw, self.cog_.starboard_self, self.cog_.starboard_jump,
                        self.cog_.starboard_autostar, self.cog_.starboard_lock, self.cog_.starboard_blacklist, self.cog_.starboard_unblacklist, self.cog_.starboard_remove, self.cog_.starboard_emoji):
            self.assertEqual(required_permissions(command), ("manage_guild",), command.name)
        for command in (self.cog_.starboard_server, self.cog_.starboard_stats, self.cog_.starboard_show, self.cog_.starboard_random):
            self.assertEqual(required_permissions(command), (), command.name)


READER = ("view_channel", "read_message_history", "send_messages", "embed_links")


class RoleChannel(FakeChannel):
    """A channel only the listed roles can open, with @everyone counted as a role."""

    def __init__(self, guild, name, *roles, **kwargs):
        super().__init__(guild, name, **kwargs)
        self.readers = {role.id for role in roles}

    def permissions_for(self, target):
        holds = [target] if isinstance(target, FakeRole) else target.roles
        if any("administrator" in role.permissions.names for role in holds):
            return FakePermissions("administrator")
        return FakePermissions(*READER) if any(role.id in self.readers for role in holds) else FakePermissions()


class AudienceCase(StarboardCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.verified = self.guild.role("Verified", 2)
        self.staff = self.guild.role("Staff", 3)
        self.everyone = self.guild.default_role
        self.board = RoleChannel(self.guild, "board", self.everyone)
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": self.board.id, "threshold": 1})

    def room(self, *roles, **kwargs):
        return RoleChannel(self.guild, "room", *roles, **kwargs)

    async def starred(self, channel):
        message = self.say(self.member, "ban list", channel=channel)
        await self.star(self.alice, message=message)
        return message


class AudienceTest(AudienceCase):
    async def test_a_hidden_channel_stays_off_a_public_board(self):
        await self.starred(self.room(self.staff))
        self.assertEqual(self.board.sent, [])

    async def test_a_public_channel_reaches_a_public_board(self):
        await self.starred(self.room(self.everyone))
        self.assertEqual(len(self.board.sent), 1)

    async def test_a_verified_only_server_isnt_read_as_hidden(self):
        self.board.readers = {self.verified.id}
        await self.starred(self.room(self.verified))
        self.assertEqual(len(self.board.sent), 1)

    async def test_a_staff_board_takes_from_staff_channels(self):
        self.board.readers = {self.staff.id}
        await self.starred(self.room(self.staff))
        self.assertEqual(len(self.board.sent), 1)

    async def test_a_staff_channel_stays_off_a_board_with_more_people_on_it(self):
        self.board.readers = {self.staff.id, self.verified.id}
        await self.starred(self.room(self.staff))
        self.assertEqual(self.board.sent, [])

    async def test_a_channel_opted_in_goes_up_anyway(self):
        room = self.room(self.staff)
        await self.bot.settings.save(self.guild.id, "starboard", {"allow_private": [room.id]})
        await self.starred(room)
        self.assertEqual(len(self.board.sent), 1)

    async def test_a_category_opted_in_covers_what_is_in_it(self):
        category = FakeChannel(self.guild, "staff", discord.ChannelType.category)
        room = self.room(self.staff, category=category)
        await self.bot.settings.save(self.guild.id, "starboard", {"allow_private": [category.id]})
        await self.starred(room)
        self.assertEqual(len(self.board.sent), 1)

    async def test_a_private_thread_never_counts_even_when_its_parent_is_opted_in(self):
        parent = self.room(self.everyone)
        thread = self.room(self.everyone, kind=discord.ChannelType.private_thread)
        thread.parent_id = parent.id
        await self.bot.settings.save(self.guild.id, "starboard", {"allow_private": [parent.id]})
        await self.starred(thread)
        self.assertEqual(self.board.sent, [])

    async def test_a_bots_role_seeing_the_board_changes_nothing(self):
        other_bot = self.guild.role("Other Bot", 2, managed=True)
        self.board.readers = {self.verified.id, other_bot.id}
        await self.starred(self.room(self.verified))
        self.assertEqual(len(self.board.sent), 1)

    async def test_a_boost_role_seeing_the_board_counts(self):
        booster = self.guild.role("Booster", 2, managed=True)
        booster.premium = True
        self.board.readers = {self.verified.id, booster.id}
        await self.starred(self.room(self.verified))
        self.assertEqual(self.board.sent, [])

    async def test_seeing_a_channel_without_its_history_isnt_reading_it(self):
        class NoHistory(RoleChannel):
            def permissions_for(self, target):
                return FakePermissions("view_channel", "send_messages")
        await self.starred(NoHistory(self.guild, "lobby", self.everyone))
        self.assertEqual(self.board.sent, [])


class StatsReachTest(SiteCase):
    modules = [Starboard]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Starboard)
        self.staff = self.guild.role("Staff", 3)
        self.helper = FakeMember(self.guild, "helper", self.guild.role("Helper", 3, "manage_guild"), self.staff)
        self.outsider = FakeMember(self.guild, "outsider", self.guild.role("Outsider", 3, "manage_guild"))
        self.everyone = self.guild.default_role
        self.board = RoleChannel(self.guild, "board", self.staff)
        self.private = RoleChannel(self.guild, "mod-chat", self.staff)
        self.public = RoleChannel(self.guild, "general", self.everyone)
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": self.board.id, "threshold": 1})
        self.posted = FakeMessage(self.private, self.admin, "||hidden|| private staff text")
        self.private.sent.append(self.posted)
        await self.cog_.on_raw_reaction_add(SimpleNamespace(
            guild_id=self.guild.id, channel_id=self.private.id, message_id=self.posted.id, user_id=self.helper.id, member=self.helper,
            message_author_id=self.admin.id, emoji=discord.PartialEmoji(name=STAR),
        ))

    async def shown(self, author, channel):
        ctx = self.context(author, channel)
        await self.cog_.starboard_server.callback(self.cog_, ctx)
        return [field.value for field in ctx.sent[0]["embed"].fields if field.name == "Most starred"][0]

    async def test_a_preview_is_for_someone_who_can_read_it_where_only_they_can(self):
        self.assertIn("private staff text", await self.shown(self.helper, self.private))
        self.assertNotIn("private staff text", await self.shown(self.outsider, self.private))
        self.assertNotIn("private staff text", await self.shown(self.helper, self.public))

    async def test_one_person_stats_follow_the_same_rule(self):
        ctx = self.context(self.outsider, self.public)
        await self.cog_.starboard_stats.callback(self.cog_, ctx, self.admin)
        fields = {field.name: field.value for field in ctx.sent[0]["embed"].fields}
        self.assertNotIn("private staff text", fields["Most starred"])

    async def test_the_page_leaves_out_what_they_cant_read(self):
        client = await self.site(self.outsider)
        self.assertEqual((await (await client.get(self.path("/starboard/stats"))).json())["posts"], [])
        client = await self.site(self.helper)
        self.assertEqual(len((await (await client.get(self.path("/starboard/stats"))).json())["posts"]), 1)

    async def test_a_spoiler_stays_covered_in_a_stored_preview(self):
        row = await self.db.one("SELECT preview FROM starboard_entries")
        self.assertEqual(row["preview"], "[spoiler] private staff text")


class ImagesOnTheBoardTest(StarboardCase):
    async def post_for(self, message):
        await self.bot.settings.save(self.guild.id, "starboard", {"threshold": 1})
        await self.star(self.alice, message=message)
        return self.board.sent[-1]

    def image_embed(self, url):
        return discord.Embed.from_dict({"type": "image", "url": url, "thumbnail": {"url": url + "?small"}})

    async def test_a_spoilered_link_doesnt_open_on_the_board(self):
        post = await self.post_for(self.say(self.member, "||https://i.example/x.png||", embeds=[self.image_embed("https://i.example/x.png")]))
        self.assertIsNone(post.embeds[0].image.url)

    async def test_a_spoilered_link_among_words_doesnt_either(self):
        post = await self.post_for(self.say(self.member, "look || this https://i.example/x.png one || ok", embeds=[self.image_embed("https://i.example/x.png")]))
        self.assertIsNone(post.embeds[0].image.url)

    async def test_a_plain_link_beside_a_spoiler_still_opens(self):
        post = await self.post_for(self.say(self.member, "||secret|| https://i.example/y.png", embeds=[self.image_embed("https://i.example/y.png")]))
        self.assertEqual(post.embeds[0].image.url, "https://i.example/y.png?small")

    async def test_an_nsfw_image_leaves_no_link_behind(self):
        lewd = FakeChannel(self.guild, "lewd", nsfw=True)
        post = await self.post_for(self.say(self.member, "Look", channel=lewd, attachments=[FakeAttachment("a.png"), FakeAttachment("b.png"), FakeAttachment("c.zip", "application/zip")]))
        field = post.embeds[0].fields[0]
        self.assertEqual(field.name, "Attachments")
        self.assertNotIn("a.png", field.value)
        self.assertNotIn("b.png", field.value)
        self.assertIn("2 images hidden", field.value)
        self.assertIn("c.zip", field.value)

    async def test_one_nsfw_image_reads_as_one(self):
        lewd = FakeChannel(self.guild, "lewd", nsfw=True)
        post = await self.post_for(self.say(self.member, "Look", channel=lewd, attachments=[FakeAttachment("a.png")]))
        self.assertEqual(post.embeds[0].fields[0].value, "1 image hidden")

    async def test_a_forward_shows_what_it_carries(self):
        message = self.say(self.member, "")
        message.message_snapshots = [SimpleNamespace(content="carried text", attachments=[FakeAttachment("f.png")], embeds=[], stickers=[])]
        post = await self.post_for(message)
        self.assertEqual(post.embeds[0].description, "carried text")
        self.assertEqual(post.embeds[0].image.url, "https://cdn.example/f.png")
        self.assertEqual((await self.entry(message))["preview"], "carried text")


class FakeReaction:
    def __init__(self, emoji, *people, count=None):
        self.emoji = emoji
        self.people = people
        self.count = len(people) if count is None else count
        self.asked = []

    async def users(self, *, limit=None, after=None):
        self.asked.append(limit)
        for person in self.people:
            yield person


class EarlierStarsTest(StarboardCase):
    async def test_stars_from_before_count_when_the_first_new_one_lands(self):
        robot = FakeMember(self.guild, "robot", bot=True)
        self.message.reactions = [FakeReaction("\N{THUMBS UP SIGN}", self.alice), FakeReaction(STAR, self.alice, self.bob, robot, self.member)]
        await self.star(self.carol)
        entry = await self.entry()
        self.assertEqual(entry["stars"], 3)  # alice, bob and carol: a bot and the author's own star don't count
        self.assertEqual(len(self.board.sent), 1)

    async def test_with_self_stars_on_the_author_counts_too(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"star_self": True, "threshold": 10})
        self.message.reactions = [FakeReaction(STAR, self.alice, self.member)]
        await self.star(self.carol)
        self.assertEqual((await self.entry())["stars"], 3)

    async def test_it_asks_once_for_each_message(self):
        reaction = FakeReaction(STAR, self.alice, self.bob)
        self.message.reactions = [reaction]
        await self.star(self.carol)
        await self.star(FakeMember(self.guild, "dave"))
        self.assertEqual(len(reaction.asked), 1)

    async def test_a_server_emoji_is_matched_by_its_id(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"emoji": "<:gold:123456789012345678>", "threshold": 10})
        wanted = discord.PartialEmoji(name="gold", id=123456789012345678)
        self.message.reactions = [FakeReaction(STAR, self.bob), FakeReaction(wanted, self.alice, count=2)]
        await self.star(self.carol, emoji=wanted)
        self.assertEqual((await self.entry())["stars"], 2)

    async def test_a_failed_read_leaves_the_star_it_has(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"threshold": 10})

        async def refuse(message_id):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.fetch_message = refuse
        await self.star(self.carol)
        self.assertEqual((await self.entry())["stars"], 1)


class DeleteDuringPostTest(StarboardCase):
    async def test_the_post_comes_down_with_the_message(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"threshold": 1})
        real = self.board.send
        deleting = []

        async def send(*args, **kwargs):
            deleting.append(asyncio.ensure_future(self.cog_._messages_deleted(self.guild.id, {self.message.id})))
            await asyncio.wait(deleting, timeout=0.3)  # holding the message's lock, the delete has to wait for this post
            return await real(*args, **kwargs)
        self.board.send = send
        await self.star(self.alice)
        await asyncio.gather(*deleting)
        self.assertEqual(self.board.sent, [])
        self.assertIsNone(await self.entry())


class ChannelNamesTest(StarboardCase):
    def named(self, words):
        return [c.name for c in self.cog_._channels_named(self.context(), words)]

    async def test_a_digit_in_a_name_is_still_a_name(self):
        FakeChannel(self.guild, "gen2")
        self.assertEqual(self.named("gen2"), ["gen2"])
        self.assertEqual(self.named("#gen2"), ["gen2"])

    async def test_ids_and_mentions_still_work(self):
        self.assertEqual(self.named(f"<#{self.channel.id}>"), ["general"])
        self.assertEqual(self.named(str(self.channel.id)), ["general"])

    async def test_a_category_can_be_named(self):
        FakeChannel(self.guild, "staff", discord.ChannelType.category)
        self.assertEqual(self.named("staff"), ["staff"])


class EmojiValueTest(BotTest):
    def test_text_that_cant_be_a_reaction_is_refused(self):
        for value in ("!!!", "<>", "--", "abc", "12", "\u00e9", "\u00b2"):
            with self.assertRaises(Invalid, msg=value):
                _emoji_value(value)

    def test_emoji_pass(self):
        for value in (STAR, "\N{GLOWING STAR}", "1\ufe0f\u20e3", "\U0001F1FA\U0001F1F8", "<:gold:123456789012345678>", "\u2757\ufe0f"):
            self.assertEqual(_emoji_value(value), value)


class BoardProblemsTest(SiteCase):
    modules = [Starboard]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Starboard)
        self.manager_role.permissions = FakePermissions("manage_guild", "view_channel", "read_message_history")
        self.alice = FakeMember(self.guild, "alice")
        self.board = FakeChannel(self.guild, "board")
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": self.board.id, "threshold": 1})
        self.posted = FakeMessage(self.channel, self.member, "good one")
        self.channel.sent.append(self.posted)

    async def star(self):
        await self.cog_.on_raw_reaction_add(SimpleNamespace(
            guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.posted.id, user_id=self.alice.id, member=self.alice,
            message_author_id=self.member.id, emoji=discord.PartialEmoji(name=STAR),
        ))

    async def status(self):
        client = await self.site(self.admin)
        return await (await client.get(self.path("/starboard/status"))).json()

    async def test_a_refused_post_shows_on_the_page_and_a_good_one_clears_it(self):
        real = self.board.send

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.board.send = refuse
        await self.star()
        status = await self.status()
        self.assertIn("can't post", status["last_error"])
        self.assertGreater(status["last_error_at"], 0)
        self.board.send = real
        await self.star()
        await self.cog_.on_raw_reaction_add(SimpleNamespace(
            guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.posted.id, user_id=self.mod.id, member=self.mod,
            message_author_id=self.member.id, emoji=discord.PartialEmoji(name=STAR),
        ))
        self.assertIsNone((await self.status())["last_error"])

    async def test_the_page_says_when_the_bot_cant_post_there(self):
        self.assertIsNone((await self.status())["problem"])
        self.role_bot.permissions = FakePermissions("view_channel")
        self.assertIn("Send Messages", (await self.status())["problem"])

    async def test_a_board_that_is_a_category_or_gone_says_so(self):
        category = FakeChannel(self.guild, "cat", discord.ChannelType.category)
        real = self.guild.get_channel_or_thread
        self.guild.get_channel_or_thread = lambda channel_id: object() if channel_id == category.id else real(channel_id)  # a real category has no send
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": category.id})
        self.assertIn("can't hold messages", (await self.status())["problem"])
        await self.star()
        self.assertIn("can't hold messages", (await self.status())["last_error"])
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": 111222333444555666})
        self.assertIn("gone", (await self.status())["problem"])

    async def test_setup_warns_when_the_bot_cant_post_there(self):
        self.role_bot.permissions = FakePermissions("view_channel")
        ctx = self.context(self.admin)
        await self.cog_.starboard_setup.callback(self.cog_, ctx, self.board, name="starboard")
        self.assertIn("Send Messages", ctx.oks[0])

    async def test_setup_answers_like_the_page(self):
        ctx = self.context(self.manager)
        await self.cog_.starboard_setup.callback(self.cog_, ctx, self.channel, name="starboard")
        self.assertEqual(ctx.fails, ["You can't post in that channel."])
        self.assertEqual(self.settings("starboard")["channel_id"], self.board.id)
        client = await self.site(self.manager)
        response = await client.put(self.path("/settings/starboard"), json={"channel_id": str(self.channel.id)})
        self.assertEqual(response.status, 400)
        self.assertIn("can't post", (await response.json())["error"])

    async def test_making_a_channel_takes_manage_channels(self):
        await self.bot.settings.save(self.guild.id, "starboard", {"channel_id": None})
        ctx = self.context(self.manager)
        await self.cog_.starboard_setup.callback(self.cog_, ctx, None, name="new:brand-new")
        self.assertIn("Manage Channels", ctx.fails[0])
        client = await self.site(self.manager)
        self.assertEqual((await client.post(self.path("/starboard/create"), json={"name": "brand-new"})).status, 403)

    async def test_without_message_content_nothing_goes_up_blank(self):
        self.bot.reads_message_text = False
        await self.star()
        self.assertEqual(self.board.sent, [])
        self.assertIn("message text", (await self.status())["last_error"])

    async def test_without_message_content_an_edit_doesnt_blank_a_post(self):
        await self.star()
        before = self.board.sent[0].embeds[0].description
        self.bot.reads_message_text = False
        self.posted.content = ""
        await self.cog_.on_raw_message_edit(SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel.id, message_id=self.posted.id))
        self.assertEqual(self.board.sent[0].embeds[0].description, before)
