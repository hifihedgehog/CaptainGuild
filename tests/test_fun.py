import json
import time
import types
from unittest import mock

import discord

from captainguild.core.outside import OutsideError, Reply
from captainguild.modules import fun as fun_module
from captainguild.modules.fun import (
    BOLD_FANCY,
    BOLD_FRAKTUR,
    DOUBLE,
    EchoView,
    EIGHT_BALL,
    FANCY,
    FRAKTUR,
    FULL_WIDTH,
    SMALL_CAPS,
    Fun,
    TopRollView,
    emojify,
    image_kind,
    owofy,
    parse_roll_range,
    pick_from,
    usable_image,
    wrap_words,
)
from captainguild.modules.moderation import Moderation
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMessage, FakePermissions
from tests.gateway import GatewayTest

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
NO_PINGS = discord.AllowedMentions.none().to_dict()


class FakeOutside:
    """Answers each address from a table. Nothing here can reach the internet."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    async def get(self, url, *, params=None, headers=None, max_bytes=0, timeout=0.0, stranger=False):
        self.calls.append({"url": url, "params": params, "stranger": stranger})
        answer = self.answers.get(url, OutsideError("Not in the test's table."))
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self):
        pass


def good(data):
    return Reply(200, json.dumps(data).encode())


class Press:
    def __init__(self, user, guild, message):
        self.user = user
        self.guild_id = guild.id
        self.guild = guild
        self.message = message
        self.sent = []
        self.edits = []
        self.response = types.SimpleNamespace(send_message=self._send, edit_message=self._edit)

    async def _send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})

    async def _edit(self, **kwargs):
        self.edits.append(kwargs)


class TextToysTest(BotTest):
    def test_every_alphabet_matches_the_published_examples(self):
        self.assertEqual("bold is beautiful".translate(BOLD_FANCY), "\U0001d4eb\U0001d4f8\U0001d4f5\U0001d4ed \U0001d4f2\U0001d4fc \U0001d4eb\U0001d4ee\U0001d4ea\U0001d4fe\U0001d4fd\U0001d4f2\U0001d4ef\U0001d4fe\U0001d4f5")
        self.assertEqual("makes text".translate(BOLD_FRAKTUR), "\U0001d592\U0001d586\U0001d590\U0001d58a\U0001d598 \U0001d599\U0001d58a\U0001d59d\U0001d599")
        self.assertEqual("makes text".translate(FRAKTUR), "\U0001d52a\U0001d51e\U0001d528\U0001d522\U0001d530 \U0001d531\U0001d522\U0001d535\U0001d531")
        self.assertEqual("Woah, double struck text".translate(DOUBLE), "\U0001d54e\U0001d560\U0001d552\U0001d559, \U0001d555\U0001d560\U0001d566\U0001d553\U0001d55d\U0001d556 \U0001d564\U0001d565\U0001d563\U0001d566\U0001d554\U0001d55c \U0001d565\U0001d556\U0001d569\U0001d565")
        self.assertEqual("damn this is fancy".translate(FANCY), "\U0001d4b9\U0001d4b6\U0001d4c2\U0001d4c3 \U0001d4c9\U0001d4bd\U0001d4be\U0001d4c8 \U0001d4be\U0001d4c8 \U0001d4bb\U0001d4b6\U0001d4c3\U0001d4b8\U0001d4ce")
        self.assertEqual("neumann".translate(SMALL_CAPS), "\u0274\u1d07\u1d1c\u1d0d\u1d00\u0274\u0274")
        self.assertEqual("600 iq".translate(FULL_WIDTH), "\uff16\uff10\uff10\u3000\uff49\uff51")

    def test_letters_unicode_placed_elsewhere_are_still_mapped(self):
        self.assertEqual("H".translate(FANCY), "\u210b")
        self.assertEqual("e".translate(FANCY), "\u212f")
        self.assertEqual("C".translate(FRAKTUR), "\u212d")
        self.assertEqual("R".translate(DOUBLE), "\u211d")
        self.assertEqual("7".translate(DOUBLE), "\U0001d7df")

    def test_anything_else_passes_through(self):
        self.assertEqual("1! \N{SNOWMAN}".translate(BOLD_FANCY), "1! \N{SNOWMAN}")

    def test_clap_and_space(self):
        self.assertEqual(wrap_words("how dare you", "\N{CLAPPING HANDS SIGN}"), "\N{CLAPPING HANDS SIGN}how\N{CLAPPING HANDS SIGN}dare\N{CLAPPING HANDS SIGN}you\N{CLAPPING HANDS SIGN}")
        self.assertEqual(wrap_words("This is a test.", "\N{WHITE HEAVY CHECK MARK}"), "\N{WHITE HEAVY CHECK MARK}This\N{WHITE HEAVY CHECK MARK}is\N{WHITE HEAVY CHECK MARK}a\N{WHITE HEAVY CHECK MARK}test.\N{WHITE HEAVY CHECK MARK}")

    def test_emojify_keeps_letters_from_joining_into_flags(self):
        text = emojify("us 1!")
        self.assertIn("\U0001f1fa \U0001f1f8", text)
        self.assertIn("1\ufe0f\u20e3", text)
        self.assertTrue(text.endswith("!"))

    def test_owofy(self):
        self.assertEqual(owofy("not my proudest work"), "nyot my pwoudest wowk")
        self.assertEqual(owofy("LOVE"), "WOVE")
        self.assertEqual(owofy("love"), "wuv")

    def test_pick_takes_commas_for_longer_choices(self):
        self.assertEqual(pick_from("red green"), ["red", "green"])
        self.assertEqual(pick_from("dark red, light green,"), ["dark red", "light green"])
        self.assertEqual(pick_from("  "), [])

    def test_roll_ranges(self):
        self.assertEqual(parse_roll_range(None), (1, 100))
        self.assertEqual(parse_roll_range("20"), (1, 20))
        self.assertEqual(parse_roll_range("5-50"), (5, 50))
        self.assertIsNone(parse_roll_range("50-5"))
        self.assertIsNone(parse_roll_range("lots"))

    def test_image_kinds_come_from_the_bytes(self):
        self.assertEqual(image_kind(PNG), "png")
        self.assertEqual(image_kind(b"\xff\xd8\xff123"), "jpeg")
        self.assertEqual(image_kind(b"GIF89a123"), "gif")
        self.assertEqual(image_kind(b"RIFFxxxxWEBPxxxx"), "webp")
        self.assertIsNone(image_kind(b"<html>"))

    def test_usable_image_links(self):
        self.assertEqual(usable_image("http://a.example/1.jpg"), "https://a.example/1.jpg")
        self.assertEqual(usable_image("https://a.example/1.jpg"), "https://a.example/1.jpg")
        self.assertIsNone(usable_image("https://a.example/1.mp4"))
        self.assertIsNone(usable_image("ftp://a.example/1.jpg"))
        self.assertIsNone(usable_image(None))


class FunCommandsTest(BotTest):
    modules = [Fun]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Fun)
        self.cog_.outside = FakeOutside()

    async def test_text_commands_never_ping(self):
        commands_ = [self.cog_.fun_aesthetics, self.cog_.fun_boldfancy, self.cog_.fun_boldfraktur, self.cog_.fun_clap, self.cog_.fun_double,
                     self.cog_.fun_emojify, self.cog_.fun_fancy, self.cog_.fun_fraktur, self.cog_.fun_owofy, self.cog_.fun_smallcaps]
        for command in commands_:
            ctx = self.context()
            await command.callback(self.cog_, ctx, sentence="hello @everyone")
            self.assertEqual(ctx.sent[-1]["allowed_mentions"].to_dict(), NO_PINGS, command.name)
            self.assertEqual(command.callback.__required_permissions__, ("manage_guild",), command.name)
        ctx = self.context()
        await self.cog_.fun_pick.callback(self.cog_, ctx, sentence="@everyone")
        self.assertEqual(ctx.sent[-1]["allowed_mentions"].to_dict(), NO_PINGS)
        await self.cog_.fun_eightball.callback(self.cog_, ctx, question="<@&1>?")
        self.assertEqual(ctx.sent[-1]["allowed_mentions"].to_dict(), NO_PINGS)
        await self.cog_.fun_space.callback(self.cog_, ctx, "@here", sentence="a b")
        self.assertEqual(ctx.sent[-1]["allowed_mentions"].to_dict(), NO_PINGS)

    async def test_long_output_is_cut_to_what_discord_takes(self):
        ctx = self.context()
        await self.cog_.fun_clap.callback(self.cog_, ctx, sentence="word " * 1000)
        self.assertLessEqual(len(ctx.sent[-1]["content"]), 2000)

    async def test_empty_output_is_refused(self):
        ctx = self.context()
        await self.cog_.fun_clap.callback(self.cog_, ctx, sentence="   ")
        self.assertTrue(ctx.fails)

    async def test_coinflip_eightball_and_roll_need_no_permission(self):
        for command in (self.cog_.fun_coinflip, self.cog_.fun_eightball, self.cog_.fun_roll):
            self.assertFalse(hasattr(command.callback, "__required_permissions__"), command.name)
        ctx = self.context(author=self.member)
        await self.cog_.fun_coinflip.callback(self.cog_, ctx)
        self.assertIn(ctx.sent[-1]["content"], ("Heads.", "Tails."))
        await self.cog_.fun_eightball.callback(self.cog_, ctx, question="Will it print?")
        self.assertTrue(any(answer in ctx.sent[-1]["content"] for answer in EIGHT_BALL))
        await self.cog_.fun_roll.callback(self.cog_, ctx, "3-3")
        self.assertIn("rolls 3", ctx.sent[-1]["content"])
        await self.cog_.fun_roll.callback(self.cog_, ctx, "junk")
        self.assertTrue(ctx.fails)

    async def test_urban_dictionary_is_for_nsfw_channels_only(self):
        ctx = self.context()
        await self.cog_.fun_urbandictionary.callback(self.cog_, ctx, word="hello")
        self.assertIn("NSFW", ctx.fails[0])
        self.assertEqual(self.cog_.outside.calls, [])

    async def test_urban_dictionary_shows_the_top_entry_without_brackets(self):
        adult = FakeChannel(self.guild, "adult", nsfw=True)
        self.cog_.outside.answers["https://api.urbandictionary.com/v0/define"] = good({"list": [
            {"word": "hello", "definition": "a [low] one", "example": "x", "permalink": "https://www.urbandictionary.com/1", "thumbs_up": 1, "thumbs_down": 0},
            {"word": "hello", "definition": "the [best] one", "example": "say [hi]", "permalink": "https://www.urbandictionary.com/2", "thumbs_up": 50, "thumbs_down": 2},
        ]})
        ctx = self.context(channel=adult)
        await self.cog_.fun_urbandictionary.callback(self.cog_, ctx, word="hello @everyone")
        embed = ctx.sent[-1]["embed"]
        self.assertEqual(embed.description, "the best one")
        self.assertEqual(embed.fields[0].value, "say hi")
        self.assertEqual(embed.url, "https://www.urbandictionary.com/2")
        self.assertEqual(ctx.sent[-1]["allowed_mentions"].to_dict(), NO_PINGS)
        self.assertEqual(self.cog_.outside.calls[0]["params"], {"term": "hello @everyone"})

    async def test_urban_dictionary_failures_tell_the_person_and_never_raise(self):
        adult = FakeChannel(self.guild, "adult", nsfw=True)
        ctx = self.context(channel=adult)
        await self.cog_.fun_urbandictionary.callback(self.cog_, ctx, word="hello")
        self.assertIn("didn't answer", ctx.fails[0])
        self.cog_.outside.answers["https://api.urbandictionary.com/v0/define"] = good({"list": []})
        await self.cog_.fun_urbandictionary.callback(self.cog_, ctx, word="hello")
        self.assertIn("No definition", ctx.fails[1])
        self.cog_.outside.answers["https://api.urbandictionary.com/v0/define"] = Reply(500, b"oops")
        await self.cog_.fun_urbandictionary.callback(self.cog_, ctx, word="hello")
        self.assertEqual(len(ctx.fails), 3)
        self.cog_.outside.answers["https://api.urbandictionary.com/v0/define"] = Reply(200, b"not json")
        await self.cog_.fun_urbandictionary.callback(self.cog_, ctx, word="hello")
        self.assertEqual(len(ctx.fails), 4)

    async def test_cute_uses_the_second_source_when_the_first_is_down(self):
        self.cog_.outside.answers.update({
            "https://dog.ceo/api/breeds/image/random": OutsideError("down"),
            "https://random.dog/woof.json": good({"url": "https://random.dog/a.jpg"}),
        })
        for _ in range(8):  # the order is random, so this covers both
            ctx = self.context()
            await self.cog_.cute.callback(self.cog_, ctx, "dog")
            self.assertEqual(ctx.sent[-1]["embeds"][0].image.url, "https://random.dog/a.jpg")

    async def test_cute_handles_a_changed_answer_and_a_video(self):
        self.cog_.outside.answers.update({
            "https://dog.ceo/api/breeds/image/random": good({"different": "shape"}),
            "https://random.dog/woof.json": good({"url": "https://random.dog/clip.mp4"}),
        })
        ctx = self.context()
        await self.cog_.cute.callback(self.cog_, ctx, "dog")
        self.assertTrue(ctx.fails)
        self.assertEqual(ctx.sent, [])

    async def test_cute_with_every_service_down_says_so(self):
        for choice in ("cat", "dog", "fox", "duck", "aww"):
            ctx = self.context()
            await self.cog_.cute.callback(self.cog_, ctx, choice, True)
            self.assertIn("didn't answer", ctx.fails[0], choice)

    async def test_cute_bomb_sends_up_to_five_different_pictures(self):
        counter = iter(range(100))

        class Counting(FakeOutside):
            async def get(self, url, **kwargs):
                return good({"image": f"https://randomfox.ca/images/{next(counter)}.jpg"})

        self.cog_.outside = Counting()
        ctx = self.context()
        await self.cog_.cute.callback(self.cog_, ctx, "fox", True)
        self.assertEqual(len(ctx.sent[-1]["embeds"]), 5)
        await self.cog_.cute.callback(self.cog_, ctx, "fox")
        self.assertEqual(len(ctx.sent[-1]["embeds"]), 1)

    async def test_aww_draws_from_all_the_animals(self):
        self.cog_.outside.answers.update({
            "https://api.thecatapi.com/v1/images/search": good([{"url": "https://cat.example/1.jpg"}]),
            "https://cataas.com/cat?json=true": good({"url": "https://cat.example/1.jpg"}),
            "https://dog.ceo/api/breeds/image/random": good({"message": "https://cat.example/1.jpg"}),
            "https://random.dog/woof.json": good({"url": "https://cat.example/1.jpg"}),
            "https://randomfox.ca/floof/": good({"image": "https://cat.example/1.jpg"}),
            "https://random-d.uk/api/v2/random": good({"url": "http://cat.example/1.jpg"}),
        })
        ctx = self.context()
        await self.cog_.cute.callback(self.cog_, ctx, "aww")
        self.assertEqual(ctx.sent[-1]["embeds"][0].image.url, "https://cat.example/1.jpg")

    async def test_addemoji_from_an_attachment(self):
        added = []

        async def create(*, name, image, reason=None):
            added.append((name, image))
            return f"<:{name}:1>"

        self.guild.create_custom_emoji = create
        attachment = types.SimpleNamespace(url="https://cdn.discordapp.com/attachments/1/2/a.png", size=len(PNG))
        self.cog_.outside.answers[attachment.url] = Reply(200, PNG)
        ctx = self.context()
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party", None, attachment)
        self.assertEqual(added, [("party", PNG)])
        self.assertTrue(ctx.oks)
        self.assertTrue(self.cog_.outside.calls[0]["stranger"], "someone else's link goes through the public-only path")

    async def test_addemoji_takes_an_emoji_an_id_or_a_link(self):
        source = self.cog_._emoji_source
        self.assertEqual(source("<:wave:123456789012345678>"), "https://cdn.discordapp.com/emojis/123456789012345678.png")
        self.assertEqual(source("<a:wave:123456789012345678>"), "https://cdn.discordapp.com/emojis/123456789012345678.gif")
        self.assertEqual(source("123456789012345678"), "https://cdn.discordapp.com/emojis/123456789012345678.png")
        self.assertEqual(source("https://a.example/x.png"), "https://a.example/x.png")
        self.assertIsNone(source("just words"))
        self.assertIsNone(source(None))

    async def test_addemoji_refusals(self):
        ctx = self.context()
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "a", "https://a.example/x.png")
        self.assertIn("name", ctx.fails[-1])
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party")
        self.assertIn("image", ctx.fails[-1])
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party", None, types.SimpleNamespace(url="https://a.example/x.png", size=300 * 1024))
        self.assertIn("256 KB", ctx.fails[-1])
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party", "https://a.example/x.png")
        self.assertIn("couldn't fetch", ctx.fails[-1])
        self.cog_.outside.answers["https://a.example/x.png"] = Reply(200, b"<html>")
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party", "https://a.example/x.png")
        self.assertIn("isn't a PNG", ctx.fails[-1])
        self.cog_.outside.answers["https://a.example/x.png"] = Reply(404, b"")
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party", "https://a.example/x.png")
        self.assertIn("404", ctx.fails[-1])

    async def test_addemoji_needs_the_bots_permission(self):
        self.me.roles = [self.guild.default_role]
        ctx = self.context()
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party", "https://a.example/x.png")
        self.assertIn("Manage Expressions", ctx.fails[0])
        self.assertEqual(self.cog_.outside.calls, [])

    async def test_addemoji_discord_refusal_is_a_message(self):
        async def refuse(**kwargs):
            raise discord.HTTPException(types.SimpleNamespace(status=400, reason="bad"), "Maximum emoji reached")

        self.guild.create_custom_emoji = refuse
        self.cog_.outside.answers["https://a.example/x.png"] = Reply(200, PNG)
        ctx = self.context()
        await self.cog_.fun_addemoji.callback(self.cog_, ctx, "party", "https://a.example/x.png")
        self.assertIn("Maximum emoji", ctx.fails[0])


class OutsideGuardTest(BotTest):
    """addemoji fetches links a member gave, so its path must refuse anything private. These run the real helper."""

    async def test_private_addresses_and_odd_links_never_connect(self):
        from captainguild.core.outside import Outside, check_public_link
        for bad in ("http://127.0.0.1/x.png", "http://10.0.0.1/x.png", "http://[::1]/x.png", "http://169.254.169.254/latest", "ftp://a.example/x.png", "http://a.example:8080/x.png", "http://user:pw@a.example/x.png", "file:///etc/passwd"):
            with self.assertRaises(OutsideError, msg=bad):
                check_public_link(bad)
        check_public_link("https://a.example/x.png")
        outside = Outside()
        with self.assertRaises(OutsideError):
            await outside.get("http://127.0.0.1/x.png", stranger=True)
        await outside.close()


class EchoTest(BotTest):
    modules = [Fun]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Fun)
        self.other = FakeChannel(self.guild, "announcements")

    async def echo(self, message="hello @everyone", channel=None, no_button=False, author=None):
        ctx = self.context(author=author or self.admin)
        await self.cog_.echo.callback(self.cog_, ctx, channel, message=message, no_button=no_button)
        return ctx

    async def test_posts_with_no_pings_and_a_button(self):
        ctx = await self.echo(channel=self.other)
        posted = self.other.sent[-1]
        self.assertEqual(posted.content, "hello @everyone")
        self.assertEqual(posted.kwargs["allowed_mentions"].to_dict(), NO_PINGS)
        self.assertIsInstance(posted.kwargs["view"], EchoView)
        self.assertTrue(ctx.oks)

    async def test_defaults_to_this_channel(self):
        await self.echo()
        self.assertEqual(self.channel.sent[-1].content, "hello @everyone")

    async def test_the_button_is_registered_for_restarts(self):
        self.assertTrue(any(isinstance(view, EchoView) for view in self.bot.added_views))

    async def test_only_manage_server_sees_who_sent_it(self):
        await self.echo()
        posted = self.channel.sent[-1]
        ordinary = Press(self.mod, self.guild, posted)
        await self.cog_.echo_pressed(ordinary)
        self.assertIn("Only people with Manage Server", ordinary.sent[0]["content"])
        boss = Press(self.owner, self.guild, posted)
        await self.cog_.echo_pressed(boss)
        self.assertIn(str(self.admin.id), boss.sent[0]["content"])
        self.assertTrue(boss.sent[0]["ephemeral"])
        self.assertEqual(boss.sent[0]["allowed_mentions"].to_dict(), NO_PINGS)

    async def test_the_button_on_another_servers_message_finds_nothing(self):
        await self.echo()
        posted = self.channel.sent[-1]
        other = FakeGuild("Other")
        pressed = Press(self.owner, other, posted)
        await self.cog_.echo_pressed(pressed)
        self.assertIn("no record", pressed.sent[0]["content"])

    async def test_no_button_without_a_modlog_keeps_the_button(self):
        ctx = await self.echo(no_button=True)
        self.assertIn("view", self.channel.sent[-1].kwargs)
        self.assertIn("modlog", ctx.oks[0])

    async def test_you_cannot_post_where_you_cannot_post(self):
        self.other.permissions_for = lambda member: FakePermissions("view_channel")
        ctx = await self.echo(channel=self.other, author=self.mod)
        self.assertIn("can't post", ctx.fails[0])
        self.assertEqual(self.other.sent, [])

    async def test_a_channel_the_bot_cannot_use_is_a_message(self):
        async def forbidden(*args, **kwargs):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "Missing Access")

        self.other.send = forbidden
        ctx = await self.echo(channel=self.other)
        self.assertTrue(ctx.fails)

    async def test_too_long_is_refused(self):
        ctx = await self.echo(message="x" * 2001)
        self.assertTrue(ctx.fails)


class EchoWithModlogTest(BotTest):
    modules = [Moderation, Fun]

    async def test_no_button_with_a_modlog_leaves_it_off_and_records_who_asked(self):
        cog = self.cog(Fun)
        logs = FakeChannel(self.guild, "mod-log")
        await self.bot.settings.save(self.guild.id, "moderation", {"modlog_channel": logs.id})
        ctx = self.context()
        await cog.echo.callback(cog, ctx, None, message="quiet", no_button=True)
        self.assertNotIn("view", self.channel.sent[-1].kwargs)
        channel_id, embed, _ = self.bot.logs.posted[-1]
        self.assertEqual(channel_id, logs.id)
        self.assertIn(str(self.admin.id), embed["footer"]["text"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM echo_messages"), 0)


class TopRollTest(BotTest):
    modules = [Fun]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog(Fun)

    async def start(self, author=None):
        ctx = self.context(author=author or self.member)
        await self.cog_.games_toproll.callback(self.cog_, ctx)
        if not ctx.sent:
            return ctx, None
        sent = FakeMessage(self.channel, self.me, "")
        score = await self.db.one("SELECT * FROM toproll_scores WHERE user_id = ? ORDER BY round_id DESC", (ctx.author.id,))
        await self.db.run("UPDATE toproll_scores SET message_id = ? WHERE round_id = ? AND user_id = ?", (sent.id, score["round_id"], score["user_id"]))
        return ctx, sent

    async def test_six_tries_add_up_and_then_the_button_goes(self):
        ctx, message = await self.start()
        self.assertIsInstance(ctx.sent[-1]["view"], TopRollView)
        total = 0
        for number in range(6):
            press = Press(self.member, self.guild, message)
            with mock.patch.object(fun_module.random, "randint", return_value=10):
                await self.cog_.toproll_pressed(press)
            total += 10
            self.assertIn(f"{number + 1} of 6", press.edits[0]["embed"].description)
            self.assertEqual("view" in press.edits[0], number == 5)
        row = await self.db.one("SELECT * FROM toproll_scores")
        self.assertEqual(row["total"], total)
        press = Press(self.member, self.guild, message)
        await self.cog_.toproll_pressed(press)
        self.assertEqual((await self.db.one("SELECT total FROM toproll_scores"))["total"], total)
        self.assertIsNone(press.edits[0]["view"])

    async def test_time_running_out_ends_the_game(self):
        _, message = await self.start()
        await self.db.run("UPDATE toproll_scores SET started_at = ?", (time.time() - 61,))
        press = Press(self.member, self.guild, message)
        await self.cog_.toproll_pressed(press)
        self.assertIsNone(press.edits[0]["view"])
        self.assertEqual((await self.db.one("SELECT total FROM toproll_scores"))["total"], 0)

    async def test_someone_elses_game_cannot_be_pressed(self):
        _, message = await self.start()
        press = Press(self.mod, self.guild, message)
        await self.cog_.toproll_pressed(press)
        self.assertIn("someone else", press.sent[0]["content"])
        self.assertEqual((await self.db.one("SELECT total FROM toproll_scores"))["total"], 0)

    async def test_one_game_per_person_per_round(self):
        await self.start()
        ctx, message = await self.start()
        self.assertIsNone(message)
        self.assertIn("already", ctx.fails[0].replace("You've played", "already"))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM toproll_rounds"), 1)

    async def test_the_round_lasts_a_day_and_posts_the_result(self):
        await self.start()
        await self.db.run("UPDATE toproll_scores SET total = 99, rolls = '[99]'")
        self.assertEqual(await self.bot.timers.run_due(time.time()), 0)
        self.assertEqual(await self.bot.timers.run_due(time.time() + 86401), 1)
        post = self.channel.sent[-1]
        self.assertIn(self.member.mention, post.embeds[0].description)
        self.assertEqual(post.kwargs["allowed_mentions"].to_dict(), NO_PINGS)
        self.assertIsNotNone((await self.db.one("SELECT ended_at FROM toproll_rounds"))["ended_at"])
        await self.cog_._toproll_due({"id": 1}, {})
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_new_round_starts_after_the_old_one(self):
        await self.start()
        await self.bot.timers.run_due(time.time() + 86401)
        ctx, message = await self.start()
        self.assertIsNotNone(message)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM toproll_rounds"), 2)

    async def test_leaderboard_sorts_and_stays_in_this_server(self):
        ctx = self.context()
        await self.cog_.games_leaderboard.callback(self.cog_, ctx)
        self.assertIn("Nobody", ctx.sent[-1]["content"])
        await self.start()
        await self.start(author=self.mod)
        await self.db.run("UPDATE toproll_scores SET total = 50 WHERE user_id = ?", (self.member.id,))
        await self.db.run("UPDATE toproll_scores SET total = 80 WHERE user_id = ?", (self.mod.id,))
        other = FakeGuild("Other")
        round_id = await self.db.insert("INSERT INTO toproll_rounds (guild_id, channel_id, started_at, ends_at) VALUES (?, 1, 1, 1)", (other.id,))
        await self.db.run("INSERT INTO toproll_scores (round_id, guild_id, user_id, started_at, total) VALUES (?, ?, 5, 1, 999)", (round_id, other.id))
        await self.cog_.games_leaderboard.callback(self.cog_, ctx)
        text = ctx.sent[-1]["embed"].description
        self.assertLess(text.index(self.mod.mention), text.index(self.member.mention))
        self.assertNotIn("999", text)

    async def test_a_game_from_another_server_cannot_be_pressed(self):
        _, message = await self.start()
        other = FakeGuild("Other")
        press = Press(self.member, other, message)
        await self.cog_.toproll_pressed(press)
        self.assertIn("over", press.sent[0]["content"])

    async def test_a_deleted_channel_still_closes_the_round(self):
        await self.start()
        self.guild.channels.remove(self.channel)
        await self.bot.timers.run_due(time.time() + 86401)
        self.assertIsNotNone((await self.db.one("SELECT ended_at FROM toproll_rounds"))["ended_at"])

    async def test_muted_output_leaves_no_half_started_game(self):
        ctx = self.context()

        async def muted(*args, **kwargs):
            return None

        ctx.send = muted
        await self.cog_.games_toproll.callback(self.cog_, ctx)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM toproll_scores"), 0)

    async def test_buttons_are_registered_for_restarts(self):
        self.assertTrue(any(isinstance(view, TopRollView) for view in self.bot.added_views))


class RealLibraryTest(GatewayTest):
    """The same flows through the real library with only the network faked, so a wrong attribute or keyword can't hide behind a fake."""
    modules = [Fun]

    def calls(self, method, path):
        return [call for call in self.net.calls if call[0] == method and call[1] == path]

    async def say(self, content, user_id=None):
        self.dispatch_message(user_id=user_id or self.owner_id, content=content)
        await self.settle()
        self.assert_no_errors(content)

    async def press(self, message, user_id):
        button = message["components"][0]["components"][0]
        data = {"custom_id": button["custom_id"], "component_type": 2}
        self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=user_id, message=message))
        await self.settle()
        self.assert_no_errors(f"pressing {button.get('label')}")

    async def test_echo_and_its_button(self):
        await self.say("!echo hello @everyone")
        posted = self.net.posted[-2]
        self.assertEqual(posted["content"], "hello @everyone")
        call = self.calls("POST", "/channels/{channel_id}/messages")[-2]
        self.assertEqual(call[2]["allowed_mentions"]["parse"], [])
        await self.press(posted, self.owner_id)
        answer = self.calls("POST", "/interactions/{webhook_id}/{webhook_token}/callback")[-1]
        self.assertIn(str(self.owner_id), answer[2]["data"]["content"])
        self.assertEqual(answer[2]["data"]["flags"], 64)
        await self.press(posted, self.member_id)
        answer = self.calls("POST", "/interactions/{webhook_id}/{webhook_token}/callback")[-1]
        self.assertIn("Only people", answer[2]["data"]["content"])

    async def test_echo_into_a_named_channel(self):
        await self.say(f"!echo <#{self.log_channel['id']}> over there")
        row = await self.db.one("SELECT channel_id FROM echo_messages")
        self.assertEqual(str(row["channel_id"]), self.log_channel["id"])

    async def test_text_commands(self):
        for text in ("!fun", "!fun clap a b", "!fun aesthetics a", "!fun boldfancy a", "!fun boldfraktur a", "!fun double a", "!fun emojify a1", "!fun fancy a", "!fun fraktur a", "!fun owofy no", "!fun smallcaps a",
                     "!fun space x a b", "!fun pick a, b", "!fun choose a b", "!fun coinflip", "!fun eightball will it?", "!fun 8ball will it?", "!fun roll", "!fun roll 5-9", "!fun urbandictionary word"):
            await self.say(text)

    async def test_toproll_all_the_way_through(self):
        await self.say("!games toproll", self.member_id)
        game = self.net.posted[-1]
        self.assertTrue(game["components"])
        for _ in range(6):
            await self.press(game, self.member_id)
        score = await self.db.one("SELECT * FROM toproll_scores")
        self.assertEqual(len(json.loads(score["rolls"])), 6)
        self.assertEqual(sum(json.loads(score["rolls"])), score["total"])
        final = self.calls("POST", "/interactions/{webhook_id}/{webhook_token}/callback")[-1]
        self.assertEqual(final[2]["data"]["components"], [])
        await self.say("!games leaderboard", self.member_id)
        self.assertIn(f"<@{self.member_id}>", self.net.posted[-1]["embeds"][0]["description"])
        await self.bot.timers.run_due(time.time() + 86401)
        await self.settle()
        self.assert_no_errors("the round ending")
        self.assertIn("over", self.net.posted[-1]["content"])

    async def test_cute_with_the_service_answering(self):
        self.cog = next(m for m in self.bot.modules if isinstance(m, Fun))
        self.cog.outside = FakeOutside({"https://randomfox.ca/floof/": good({"image": "https://randomfox.ca/images/1.jpg"})})
        await self.say("!cute fox")
        self.assertEqual(self.net.posted[-1]["embeds"][0]["image"]["url"], "https://randomfox.ca/images/1.jpg")

    async def test_addemoji_with_a_real_guild(self):
        cog = next(m for m in self.bot.modules if isinstance(m, Fun))
        cog.outside = FakeOutside({"https://a.example/x.png": Reply(200, PNG)})
        await self.say("!fun addemoji party https://a.example/x.png")
        made = self.calls("POST", "/guilds/{guild_id}/emojis")[-1]
        self.assertEqual(made[2]["name"], "party")
        self.assertTrue(made[2]["image"].startswith("data:image/png;base64,"))
