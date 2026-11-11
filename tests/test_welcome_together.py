"""A join, from the event to the post, with the real greetings module and the real welcome picture module together.

Each has its own tests against a stand-in for the other. These hold the two to each other: the picture greetings attaches is the one
the picture module drew, and the picture's text shows the values the message shows.
"""
from __future__ import annotations

import io
from unittest import mock

from PIL import Image

from captainguild.core.ops import DiscordOps
from captainguild.core.welcomecard import render
from captainguild.modules.greetings import Greetings
from captainguild.modules.welcomecard import WelcomeCard
from tests.fakes import BotTest, FakeAsset, FakeMember
from tests.test_greetings import text
from tests.test_welcomecard import FakeOutside, avatar_of
from tests.test_welcomecard_render import face_png

PLAIN = {"background": {"kind": "color", "color": "#101418"}}


class Together(BotTest):
    modules = [Greetings, WelcomeCard]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        render.CACHE.clear()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.greetings = self.cog(Greetings)
        self.cards = self.cog(WelcomeCard)
        self.outside = FakeOutside()
        self.cards.outside = self.outside
        await self.bot.settings.save(self.guild.id, "greetings", {"welcome_channel": self.channel.id, "welcome": text("Hi {user}")})

    async def asyncTearDown(self) -> None:
        for task in list(self.greetings._tasks):
            if not task.done():
                await task
        await super().asyncTearDown()

    def newcomer(self, name: str = "Sam", **kwargs) -> FakeMember:
        member = FakeMember(self.guild, name, **kwargs)
        member.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{member.id}/abc.png?size=1024")
        self.outside.pictures[avatar_of(member)] = face_png()
        return member

    async def cards_set(self, **settings) -> None:
        await self.bot.settings.save(self.guild.id, "welcomecard", {**PLAIN, **settings})

    async def greetings_set(self, **settings) -> None:
        await self.bot.settings.save(self.guild.id, "greetings", settings)

    @staticmethod
    def file_in(sent) -> tuple[str, Image.Image] | None:
        kwargs = sent.kwargs if hasattr(sent, "kwargs") else sent
        file = kwargs.get("file")
        if file is None:
            return None
        file.fp.seek(0)
        return file.filename, Image.open(io.BytesIO(file.fp.read()))

    def drawn_texts(self, watched) -> list[str]:
        return [marked.text for call in watched.call_args_list for marked in call.args[1].texts.values()]


class JoinTest(Together):
    async def test_with_pictures_off_the_welcome_is_the_message_alone(self):
        await self.greetings.on_member_join(self.newcomer())
        sent = self.channel.sent[-1]
        self.assertEqual(sent.content, "Hi Sam")
        self.assertIsNone(self.file_in(sent))
        self.assertEqual(sent.embeds, [])
        self.assertEqual(self.outside.asked, [])

    async def test_a_join_posts_the_drawn_picture_inside_the_first_embed(self):
        await self.cards_set(enabled=True)
        await self.greetings_set(welcome={"content": "Hi {user}", "embeds": [{"title": "Welcome {user}"}], "buttons": []})
        await self.greetings.on_member_join(self.newcomer())
        sent = self.channel.sent[-1]
        name, picture = self.file_in(sent)
        self.assertEqual((name, picture.format, picture.size), ("welcome.png", "PNG", (1000, 300)))
        self.assertEqual((sent.content, sent.embeds[0].title), ("Hi Sam", "Welcome Sam"))
        self.assertEqual(sent.embeds[0].to_dict()["image"]["url"], "attachment://welcome.png")

    async def test_a_message_with_no_embed_gets_one_for_the_picture(self):
        await self.cards_set(enabled=True, layout="tall")
        await self.greetings.on_member_join(self.newcomer())
        sent = self.channel.sent[-1]
        name, picture = self.file_in(sent)
        self.assertEqual((name, picture.size), ("welcome.png", (750, 516)))
        self.assertEqual([embed.to_dict().get("image", {}).get("url") for embed in sent.embeds], ["attachment://welcome.png"])
        self.assertEqual(sent.content, "Hi Sam")

    async def test_outside_the_embed_the_picture_is_a_plain_attachment(self):
        await self.cards_set(enabled=True, in_embed=False)
        await self.greetings.on_member_join(self.newcomer())
        sent = self.channel.sent[-1]
        self.assertEqual(self.file_in(sent)[0], "welcome.png")
        self.assertEqual(sent.embeds, [])

    async def test_a_frame_grows_the_picture_that_is_posted(self):
        await self.cards_set(enabled=True, frame=True, frame_width=16)
        await self.greetings.on_member_join(self.newcomer())
        self.assertEqual(self.file_in(self.channel.sent[-1])[1].size, (1032, 332))

    async def test_a_picture_with_no_message_text_is_still_a_welcome(self):
        await self.cards_set(enabled=True)
        await self.greetings_set(welcome={"content": "", "embeds": [], "buttons": []})
        await self.greetings.on_member_join(self.newcomer())
        sent = self.channel.sent[-1]
        self.assertEqual(self.file_in(sent)[0], "welcome.png")
        self.assertFalse(sent.content)

    async def test_the_welcome_switch_stops_the_picture_too(self):
        await self.cards_set(enabled=True)
        await self.greetings_set(welcome_on=False)
        await self.greetings.on_member_join(self.newcomer())
        self.assertEqual(self.channel.sent, [])

    async def test_the_pictures_text_shows_what_the_message_shows(self):
        await self.cards_set(enabled=True, text="{user} is number {ord(plain):{joined}}\nof {num(dots):{server(members)}}")
        await self.greetings_set(welcome=text("{user} is number {ord(plain):{joined}}"))
        await self.greetings._set_joined(self.guild.id, 64096)
        with mock.patch.object(render, "render", wraps=render.render) as watched:
            await self.greetings.on_member_join(self.newcomer())
        self.assertEqual(self.channel.sent[-1].content, "Sam is number 64097th")
        self.assertEqual(self.drawn_texts(watched), [f"Sam is number 64097th\nof {self.guild.member_count}"])

    async def test_one_drawing_serves_the_welcome_and_the_join_dm(self):
        await self.cards_set(enabled=True, dm_image=True)
        await self.greetings_set(dm=text("Glad you came, {user}"))
        member = self.newcomer()
        with mock.patch.object(render, "render", wraps=render.render) as watched:
            await self.greetings.on_member_join(member)
        self.assertEqual(watched.call_count, 1)
        self.assertEqual(self.file_in(self.channel.sent[-1])[0], "welcome.png")
        self.assertEqual(member.dms[-1]["content"], "Glad you came, Sam")
        self.assertEqual(self.file_in(member.dms[-1])[0], "welcome.png")
        self.assertEqual(self.outside.asked, [avatar_of(member)])

    async def test_the_join_dm_has_no_picture_unless_the_server_asked_for_one(self):
        await self.cards_set(enabled=True)
        await self.greetings_set(dm=text("Glad you came, {user}"))
        member = self.newcomer()
        await self.greetings.on_member_join(member)
        self.assertEqual(self.file_in(self.channel.sent[-1])[0], "welcome.png")
        self.assertIsNone(self.file_in(member.dms[-1]))

    async def test_a_welcome_that_waits_for_the_rules_screen_draws_when_the_member_is_through(self):
        await self.cards_set(enabled=True, text="Number {joined}")
        await self.greetings_set(wait_for_rules=True)
        await self.greetings._set_joined(self.guild.id, 10)
        waiting = self.newcomer(pending=True)
        await self.greetings.on_member_join(waiting)
        await self.greetings.on_member_join(self.newcomer("Later"))  # someone else joins while the first reads the rules
        self.assertEqual([sent.content for sent in self.channel.sent], ["Hi Later"])
        through = self.newcomer("Sam")
        through.id, through.pending = waiting.id, False
        self.outside.pictures[avatar_of(through)] = face_png()
        through.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{through.id}/abc.png?size=1024")
        with mock.patch.object(render, "render", wraps=render.render) as watched:
            await self.greetings.on_member_update(waiting, through)
        self.assertEqual(self.channel.sent[-1].content, "Hi Sam")
        self.assertEqual(self.drawn_texts(watched), ["Number 11"])  # their own number, not Later's 12

    async def test_goodbyes_carry_no_picture(self):
        await self.cards_set(enabled=True)
        await self.greetings_set(farewell=text("Bye {user}"))
        member = self.newcomer()
        self.greetings.BAN_WAIT = 0
        await self.greetings.on_member_remove(member)
        self.assertEqual(self.channel.sent[-1].content, "Bye Sam")
        self.assertIsNone(self.file_in(self.channel.sent[-1]))

    async def test_an_avatar_that_can_not_be_fetched_does_not_stop_the_welcome(self):
        await self.cards_set(enabled=True)
        member = self.newcomer()
        self.outside.pictures.clear()
        await self.greetings.on_member_join(member)
        sent = self.channel.sent[-1]
        self.assertEqual((sent.content, self.file_in(sent)[1].size), ("Hi Sam", (1000, 300)))


class PictureTextTest(Together):
    """The picture reads the engine's own marking of which text came from outside, whatever ranges the engine keeps it in."""

    def filled(self, text: str, member=None):
        from captainguild.core.tagscript import Scope
        from captainguild.core.welcomecard import template
        design = {"layers": [{"id": "words", "type": "text", "hidden": False, "text": text}]}
        return template.fill(design, Scope(guild=self.guild, member=member or self.newcomer())).texts["words"]

    async def test_a_date_format_keeps_its_own_spaces_and_commas_when_the_date_is_the_members(self):
        member = self.newcomer()
        when = member.joined_at
        pieces = [("Joined ", 0), (when.strftime("%B"), 1), (" ", 0), (str(when.day), 1), (", ", 0), (when.strftime("%Y"), 1)]
        marked = self.filled("Joined {strf({user(joined_at)}):%B %-d, %Y}", member)
        self.assertEqual(marked.text, "".join(text for text, _ in pieces))
        self.assertEqual(bytes(marked.outside), b"".join(bytes([flag]) * len(text) for text, flag in pieces),
                         "the format's own words are the server's, and what the member's date gives is from outside")

    async def test_marks_inside_a_date_format_are_the_servers(self):
        from captainguild.core.welcomecard import text as words
        marked = self.filled("Since {strf({user(created_at)}):**%Y**}")
        self.assertIn("**", marked.text)
        bold = [span for span in words.parse(marked) if span.bold]
        self.assertTrue(bold and bold[0].text.isdigit(), [(span.text, span.bold) for span in words.parse(marked)])

    async def test_a_name_still_counts_as_outside_text(self):
        marked = self.filled("Hi {user}", self.newcomer("**Sam**"))
        self.assertEqual(marked.text, "Hi **Sam**")
        self.assertEqual(bytes(marked.outside), bytes(3) + bytes([1]) * 7)


class CommandsTogetherTest(Together):
    async def test_the_group_holds_both_modules_commands_within_discords_limit(self):
        names = {command.name for command in self.greetings.greetings.commands}
        self.assertLessEqual({"image", "image_dm", "image_preview", "switch", "delete_after", "joined", "mentions", "testgreet"}, names)
        slash = [command for command in self.greetings.greetings.commands if getattr(command, "app_command", None) is not None]
        self.assertLessEqual(len(slash), 25)

    async def test_the_test_command_attaches_the_drawn_picture(self):
        await self.cards_set(enabled=True)
        ctx = self.context(self.admin, self.channel)
        self.admin.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{self.admin.id}/abc.png?size=1024")
        self.outside.pictures[avatar_of(self.admin)] = face_png()
        await self.greetings.greetings_testgreet.callback(self.greetings, ctx)
        with_files = [sent for sent in ctx.sent if self.file_in(sent) is not None]
        self.assertEqual(len(with_files), 1)
        self.assertEqual(self.file_in(with_files[0])[1].size, (1000, 300))

    async def test_the_test_command_never_counts_a_join(self):
        await self.cards_set(enabled=True, text="Number {joined}")
        await self.greetings._set_joined(self.guild.id, 500)
        self.admin.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{self.admin.id}/abc.png?size=1024")
        self.outside.pictures[avatar_of(self.admin)] = face_png()
        await self.greetings.greetings_testgreet.callback(self.greetings, self.context(self.admin, self.channel))
        self.assertEqual(await self.db.scalar("SELECT joined FROM greeting_counts WHERE guild_id = ?", (self.guild.id,)), 500)
