"""The welcome picture module: what greetings gets when it asks for a picture, the pictures a server uploads, the dashboard routes and the commands."""
from __future__ import annotations

import asyncio
import base64
import io
import re
import struct
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
import discord
from aiohttp.test_utils import TestClient, TestServer
from discord.ext import commands
from PIL import Image, ImageChops

from captainguild.core import context as context_module
from captainguild.core import images
from captainguild.core import outside as outside_module
from captainguild.core.context import Context as BotContext
from captainguild.core.module import required_permissions
from captainguild.core.outside import Outside, OutsideError, Reply
from captainguild.core.tagscript import OverBudget, Scope
from captainguild.core.validate import Invalid
from captainguild.core.welcomecard import backgrounds, fonts, gif, layouts, model, render, text, uploads
from captainguild.demo import GUILD, build_demo
from captainguild.modules import welcomecard as module
from captainguild.modules.greetings import Greetings
from captainguild.modules.welcomecard import WelcomeCard, cdn_address
from tests.fakes import BotTest, FakeAsset, FakeChannel, FakeGuild, FakeMember
from tests.test_welcomecard_render import GIF_COLORS, RED, face_png, hand_gif, lzw_codes, moving_gif, near, png


class FakeOutside:
    """Stands in for the fetches to Discord's image servers. It serves what a test put in `pictures` and counts what was asked for."""

    def __init__(self) -> None:
        self.pictures: dict[str, bytes] = {}
        self.asked: list[str] = []
        self.wait = 0.0
        self.error: Exception | None = None

    async def get(self, url: str, **kwargs) -> Reply:
        self.asked.append(url)
        if self.wait:
            await asyncio.sleep(self.wait)
        if self.error is not None:
            raise self.error
        if url not in self.pictures:
            return Reply(404, b"", {}, url)
        return Reply(200, self.pictures[url], {}, url)

    async def close(self) -> None:
        pass


def avatar_of(member) -> str:
    return f"https://cdn.discordapp.com/avatars/{member.id}/abc.png?size=256"


def upload_form(data: bytes, name: str | None = None, filename: str = "picture.png") -> aiohttp.FormData:
    body = aiohttp.FormData()
    body.add_field("image", data, filename=filename, content_type="image/png")
    if name is not None:
        body.add_field("name", name, content_type="text/plain")
    return body


class Case(BotTest):
    modules = [Greetings, WelcomeCard]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        render.CACHE.clear()
        self.cards = self.cog(WelcomeCard)
        self.outside = FakeOutside()
        self.cards.outside = self.outside
        self.newcomer = self.joiner("Sam")

    def joiner(self, name: str) -> FakeMember:
        member = FakeMember(self.guild, name)
        member.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{member.id}/abc.png?size=1024")
        self.outside.pictures[avatar_of(member)] = face_png()
        return member

    async def save(self, **settings) -> dict:
        return await self.bot.settings.save(self.guild.id, "welcomecard", settings)

    def scope(self, member=None, **extra) -> Scope:
        return Scope(guild=self.guild, member=member or self.newcomer, extra=extra)

    async def picture(self, member=None, kind: str = "welcome", **extra):
        member = member or self.newcomer
        return await self.cards.picture_for(member, kind, self.scope(member, **extra))

    def watch_draws(self):
        """Lets a test see what the renderer was handed."""
        return mock.patch.object(render, "render", wraps=render.render)


class PictureForTest(Case):
    async def test_a_server_that_changed_nothing_gets_no_picture(self):
        self.assertEqual(self.settings("welcomecard")["enabled"], False)
        self.assertIsNone(await self.picture())
        self.assertIsNone(await self.picture(kind="dm"))
        self.assertEqual(self.outside.asked, [], "nothing is fetched for a picture nobody asked for")

    async def test_switched_on_it_gives_greetings_a_png_and_where_to_put_it(self):
        await self.save(enabled=True)
        picture = await self.picture()
        self.assertEqual(sorted(picture), ["data", "filename", "in_embed"])
        self.assertEqual((picture["filename"], picture["in_embed"]), ("welcome.png", True))
        image = Image.open(io.BytesIO(picture["data"]))
        self.assertEqual((image.format, image.size), ("PNG", (1000, 300)))
        self.assertEqual(self.cards.problems(self.guild.id), [])

    async def test_in_embed_follows_the_setting(self):
        await self.save(enabled=True, in_embed=False)
        self.assertIs((await self.picture())["in_embed"], False)

    async def test_the_join_dm_gets_one_only_when_that_is_switched_on(self):
        await self.save(enabled=True)
        self.assertIsNone(await self.picture(kind="dm"))
        await self.save(dm_image=True)
        self.assertEqual((await self.picture(kind="dm"))["filename"], "welcome.png")
        await self.save(enabled=False)
        self.assertIsNone(await self.picture(kind="dm"), "the DM's picture needs pictures on at all")

    async def test_an_unknown_kind_gets_nothing(self):
        await self.save(enabled=True, dm_image=True)
        self.assertIsNone(await self.picture(kind="farewell"))

    async def test_the_welcome_and_the_join_dm_share_one_drawing(self):
        await self.save(enabled=True, dm_image=True)
        with self.watch_draws() as drawn:
            welcome = await self.picture()
            dm = await self.picture(kind="dm")
        self.assertEqual(drawn.call_count, 1)
        self.assertEqual(dm, welcome)
        self.assertIsNot(dm, welcome, "each caller gets a dict of its own to change")

    async def test_in_a_burst_each_members_dm_still_reuses_their_welcomes_drawing(self):
        await self.save(enabled=True, dm_image=True)
        robin = self.joiner("Robin")
        with self.watch_draws() as drawn:
            sam = await self.picture()
            await self.picture(robin)
            self.assertEqual((await self.picture(kind="dm"))["data"], sam["data"])
            await self.picture(robin, kind="dm")
        self.assertEqual(drawn.call_count, 2, "two members are two drawings, however their four messages fall")

    async def test_a_welcome_and_a_dm_asked_at_the_same_moment_wait_for_one_drawing(self):
        await self.save(enabled=True, dm_image=True)
        with self.watch_draws() as drawn:
            welcome, dm = await asyncio.gather(self.picture(), self.picture(kind="dm"))
        self.assertEqual(drawn.call_count, 1)
        self.assertEqual(welcome["data"], dm["data"])
        self.assertEqual(self.cards._drawing, {}, "nothing is left waiting afterwards")

    async def test_a_drawing_that_fails_is_noted_once_for_both_messages(self):
        await self.save(enabled=True, dm_image=True)
        with mock.patch.object(module, "MAX_WAITING", 0):
            self.assertEqual(await asyncio.gather(self.picture(), self.picture(kind="dm")), [None, None])
        self.assertEqual(self.cards._skipped[self.guild.id][0], 1, "one member went without a picture, not two")
        self.assertEqual(self.cards._drawing, {})

    async def test_a_picture_drawn_from_settings_that_changed_meanwhile_is_not_kept(self):
        await self.save(enabled=True, background={"kind": "color", "color": "#ff0000"})
        gate = threading.Event()
        real = render.render

        def held(design, inputs):
            gate.wait(5)
            return real(design, inputs)
        with mock.patch.object(render, "render", held):
            first = asyncio.ensure_future(self.picture())
            await asyncio.sleep(0.3)
            await self.save(background={"kind": "color", "color": "#0000ff"})
            gate.set()
            old = await first
        new = await self.picture()
        self.assertNotEqual(new["data"], old["data"], "the next picture is drawn from the settings as they stand now")

    async def test_the_text_sees_the_values_greetings_passes(self):
        await self.save(enabled=True, text="{user} is member {joined} of {server}")
        with self.watch_draws() as drawn:
            await self.picture(joined="64,097")
        self.assertEqual(drawn.call_args.args[1].texts["text"].text, f"Sam is member 64,097 of {self.guild.name}")

    async def test_a_name_can_not_style_the_picture(self):
        await self.save(enabled=True, text="Welcome **{user}**")
        star = self.joiner("*Star* <:x:123456789012345678>")
        with self.watch_draws() as drawn:
            await self.picture(star)
        spans = text.parse(drawn.call_args.args[1].texts["text"])
        self.assertEqual([(span.text, span.bold, span.italic, span.emoji) for span in spans], [("Welcome ", False, False, ""), ("*Star* <:x:123456789012345678>", True, False, "")])
        self.assertFalse(any("emojis" in asked for asked in self.outside.asked), "a name's emoji text fetches nothing")

    async def test_the_avatar_is_fetched_once_as_a_small_png_and_drawn(self):
        await self.save(enabled=True, dm_image=True, background={"kind": "color", "color": "#000000"}, avatar_shape="square", ring_width=0)
        with self.watch_draws() as drawn:
            first = await self.picture()
            second = await self.picture(kind="dm")
        self.assertEqual(self.outside.asked, [avatar_of(self.newcomer)])
        self.assertEqual(drawn.call_count, 1, "the welcome and the join DM share one drawing")
        self.assertEqual(first, second)
        self.assertEqual(Image.open(io.BytesIO(first["data"])).convert("RGB").getpixel((150, 150)), (40, 90, 200))

    async def test_each_member_gets_their_own_picture(self):
        await self.save(enabled=True)
        other = self.joiner("Robin")
        self.outside.pictures[avatar_of(other)] = png(color=(10, 200, 10))
        self.assertNotEqual((await self.picture())["data"], (await self.picture(other))["data"])

    async def test_an_animated_avatar_is_asked_for_as_a_still(self):
        await self.save(enabled=True)
        self.newcomer.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{self.newcomer.id}/a_abc.gif?size=1024")
        await self.picture()
        self.assertEqual(self.outside.asked, [f"https://cdn.discordapp.com/avatars/{self.newcomer.id}/a_abc.png?size=256"])

    async def test_no_avatar_is_fetched_when_the_picture_shows_none(self):
        await self.save(enabled=True, avatar=False, background={"kind": "color", "color": "#000000"})
        self.assertIsNotNone(await self.picture())
        self.assertEqual(self.outside.asked, [])

    async def test_the_avatar_is_asked_for_at_the_size_its_largest_layer_needs(self):
        address = f"https://cdn.discordapp.com/avatars/{self.newcomer.id}/abc.png"
        large = {"width": 1200, "height": 600, "layers": [{"type": "avatar", "x": 0, "y": 0, "w": 120, "h": 120}, {"type": "avatar", "x": 300, "y": 40, "w": 500, "h": 480},
                                                           {"type": "avatar", "x": 0, "y": 0, "w": 1100, "h": 600, "hidden": True}]}
        await self.save(enabled=True, mode="design", design=large)
        await self.picture()
        self.assertEqual(self.outside.asked, [address + "?size=512"], "the 500 pixel avatar decides, and a hidden layer counts for nothing")
        self.outside.asked.clear()
        await self.save(mode="layout", avatar=False, background={"kind": "avatar"})
        await self.picture()
        self.assertEqual(self.outside.asked, [address + "?size=64"], "an avatar that only lends its color is asked for small")

    async def test_an_avatar_anywhere_but_discords_servers_is_never_fetched(self):
        await self.save(enabled=True)
        self.newcomer.display_avatar = FakeAsset("https://example.com/avatars/1/abc.png")
        self.assertIsNotNone(await self.picture())
        self.assertEqual(self.outside.asked, [])

    async def test_a_slow_or_broken_avatar_fetch_still_gives_a_picture(self):
        await self.save(enabled=True)
        for error in (OutsideError("down"), RuntimeError("anything else")):
            self.outside.error = error
            self.cards._recent.drop(lambda key: True)
            self.cards._fetched.drop(lambda key: True)
            self.assertEqual((await self.picture())["filename"], "welcome.png")

    async def test_a_failed_fetch_is_not_tried_again_for_every_join(self):
        await self.save(enabled=True, text="<:wave:123456789012345678>")
        await self.picture()
        await self.picture(self.joiner("Robin"))
        self.assertEqual(self.outside.asked.count("https://cdn.discordapp.com/emojis/123456789012345678.png?size=128"), 1)

    async def test_a_server_emoji_the_server_wrote_is_fetched_and_drawn(self):
        self.outside.pictures["https://cdn.discordapp.com/emojis/123456789012345678.png?size=128"] = png(color=(0, 255, 0))
        await self.save(enabled=True, avatar=False, background={"kind": "color", "color": "#000000"}, text="<:wave:123456789012345678>", outline_width=0)
        image = Image.open(io.BytesIO((await self.picture())["data"])).convert("RGB")
        self.assertGreater(dict((color, count) for count, color in image.getcolors(maxcolors=100000)).get((0, 255, 0), 0), 1000)

    async def test_the_event_loop_never_reads_the_texts(self):
        self.outside.pictures["https://cdn.discordapp.com/emojis/123456789012345678.png?size=128"] = png(color=(0, 255, 0))
        await self.save(enabled=True, text="Hi {user} <:wave:123456789012345678> **bold** " * 20)
        on_the_loop = []
        real = text._tidy  # every reading of a text starts here

        def watched(marked):
            on_the_loop.append(threading.current_thread() is threading.main_thread())
            return real(marked)
        with mock.patch.object(text, "_tidy", watched):
            self.assertIsNotNone(await self.picture())
        self.assertTrue(on_the_loop, "the texts were read")
        self.assertNotIn(True, on_the_loop, "every reading of a text runs on a worker thread")
        self.assertIn("https://cdn.discordapp.com/emojis/123456789012345678.png?size=128", self.outside.asked)

    async def test_it_never_raises_and_says_what_went_wrong(self):
        await self.save(enabled=True)
        with mock.patch.object(render, "render", side_effect=RuntimeError("boom")), self.assertLogs("captainguild.modules.welcomecard", "WARNING"):
            self.assertIsNone(await self.picture())
        self.assertEqual(self.cards._problems[self.guild.id], "The last welcome picture couldn't be drawn. The bot's log says why.")
        with mock.patch.object(render, "render", side_effect=render.RenderError("The picture took too long to draw. Use fewer or smaller layers.")):
            self.assertIsNone(await self.picture(self.joiner("Robin")))
        self.assertEqual(self.cards._problems[self.guild.id], "The last welcome picture wasn't drawn. The picture took too long to draw. Use fewer or smaller layers.")
        self.assertIsNotNone(await self.picture(self.joiner("Alex")))
        self.assertNotIn(self.guild.id, self.cards._problems, "a picture that draws clears the note")

    async def test_it_never_raises_for_something_that_is_no_member(self):
        with self.assertLogs("captainguild.modules.welcomecard", "WARNING"):
            self.assertIsNone(await self.cards.picture_for(None, "welcome", None))
            self.assertIsNone(await self.cards.picture_for(object(), "welcome", object()))

    async def test_a_scope_that_is_not_one_falls_back_to_the_member(self):
        await self.save(enabled=True, text="Hi {user}")
        with self.watch_draws() as drawn:
            self.assertIsNotNone(await self.cards.picture_for(self.newcomer, "welcome", None))
        self.assertEqual(drawn.call_args.args[1].texts["text"].text, "Hi Sam")

    async def test_it_answers_in_time_when_a_draw_hangs(self):
        await self.save(enabled=True)
        release = threading.Event()

        def stuck(design, inputs):
            release.wait(5)
            return render.Picture(b"late", "png", 1, 1)
        started = time.monotonic()
        with mock.patch.object(module, "PICTURE_SECONDS", 1.0), mock.patch.object(render, "render", stuck):
            self.assertIsNone(await self.picture())
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertEqual(self.cards._problems[self.guild.id], "The last welcome picture took too long to draw, so the welcome went out without it.")
            self.assertEqual(self.cards._waiting, 1, "the abandoned draw still holds its worker")
            release.set()
            for _ in range(100):
                if not self.cards._waiting:
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(self.cards._waiting, 0)

    async def test_a_burst_runs_two_draws_at_a_time_and_turns_away_what_the_queue_cant_hold(self):
        await self.save(enabled=True)
        running, most = [0], [0]
        lock = threading.Lock()

        def slow(design, inputs):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.15)
            with lock:
                running[0] -= 1
            return render.Picture(b"\x89PNG", "png", 10, 10)
        joiners = [self.joiner(f"raider{index}") for index in range(12)]
        with mock.patch.object(module, "MAX_WAITING", 5), mock.patch.object(render, "render", slow):
            results = await asyncio.gather(*(self.picture(member) for member in joiners))
        drawn = [result for result in results if result is not None]
        self.assertEqual(len(drawn), 5, "two running and three waiting get a picture, and the rest go out without one")
        self.assertEqual(most[0], 2, "never more than two draws at once, however many joined")
        self.assertEqual(self.cards.problems(self.guild.id), ["7 welcomes in the last ten minutes went out with no picture, because more people joined at once than the bot draws pictures for."])
        self.assertEqual(self.cards._waiting, 0)
        with mock.patch.object(module.time, "monotonic", return_value=time.monotonic() + module.SKIPPED_SECONDS + 1):
            self.assertEqual(self.cards.problems(self.guild.id), [], "ten minutes on, it is no longer news")

    async def test_a_burst_fetches_nothing_for_the_pictures_it_turns_away(self):
        await self.save(enabled=True)
        self.outside.wait = 0.05
        release = threading.Event()
        real = render.render

        def held(design, inputs):
            release.wait(3)
            return real(design, inputs)
        joiners = [self.joiner(f"raider{index}") for index in range(60)]
        with mock.patch.object(module, "MAX_WAITING", 5), mock.patch.object(render, "render", held):
            tasks = [asyncio.ensure_future(self.picture(member)) for member in joiners]
            await asyncio.sleep(0.5)
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(sum(1 for result in results if result is not None), 5)
        self.assertEqual(len(self.outside.asked), 5, "an avatar is fetched only for a picture that has its place")
        self.assertEqual(self.cards._waiting, 0)

    async def test_a_draw_whose_values_or_pictures_fail_gives_back_its_place(self):
        await self.save(enabled=True)
        with mock.patch.object(self.cards, "_inputs", side_effect=RuntimeError("the fetch broke")), self.assertLogs("captainguild.modules.welcomecard", "WARNING"):
            self.assertIsNone(await self.picture())
        self.assertEqual(self.cards._waiting, 0)
        blocked = asyncio.Event()

        async def stuck(*args, **kwargs):
            await blocked.wait()
        with mock.patch.object(self.cards, "_inputs", stuck), mock.patch.object(module, "PICTURE_SECONDS", 0.3):
            self.assertIsNone(await self.picture(self.joiner("Robin")))
        self.assertEqual(self.cards._waiting, 0, "a draw that gave up while its pictures were fetched gives back its place at once")
        self.assertEqual(self.cards._joins.by_server, {}, "and its server's count of places")

    async def test_a_join_that_waited_for_a_worker_until_little_time_was_left_gets_its_picture(self):
        row = await self.cards.store_upload(self.guild.id, moving_gif(480, 270, frames=20), "Moving")
        await self.save(enabled=True, background={"kind": "upload", "id": row["id"]})
        self.assertEqual((await self.picture(self.joiner("Warm")))["filename"], "welcome.gif")  # the fitted frames are now cached
        held = [self.joiner("Ann"), self.joiner("Ben")]
        late = self.joiner("Cal")
        real = render.render

        def slow(design, inputs):
            if inputs.person in {member.id for member in held}:
                time.sleep(2.4)  # both workers stay busy until the late join has about a second and a half left
            return real(design, inputs)
        with mock.patch.object(module, "PICTURE_SECONDS", 4.0), mock.patch.object(render, "render", slow):
            tasks = [asyncio.ensure_future(self.picture(member)) for member in held]
            await asyncio.sleep(0.05)
            tasks.append(asyncio.ensure_future(self.picture(late)))
            results = await asyncio.gather(*tasks)
        self.assertIsNotNone(results[2], self.cards.problems(self.guild.id))
        self.assertEqual(results[2]["filename"], "welcome.gif", "the kept frames draw in a fraction of the time left")

    async def test_a_name_ending_in_a_joiner_gets_its_picture_and_the_preview_one_too(self):
        await self.save(enabled=True, text="Welcome {user}\nto {server}!")
        viewer = self.joiner("Sam" + chr(0x200D))
        picture = await self.cards._draw(self.guild.id, self.settings("welcomecard"), self.scope(viewer), person=viewer.id, avatar="", pool="previews",
                                         give_up=time.monotonic() + 20, max_bytes=8 << 20)
        self.assertEqual(picture.kind, "png")
        self.assertIsNotNone(await self.picture(viewer), self.cards.problems(self.guild.id))

    async def test_one_servers_previews_leave_room_for_another_servers(self):
        from captainguild.core.tagscript import budget
        await self.save(enabled=True)
        other = FakeGuild("Other Server")
        self.bot.fake_guilds.append(other)
        release = threading.Event()
        real = render.render
        order: list[int] = []

        def held(design, inputs):
            order.append(inputs.person)
            if len(order) == 1:
                release.wait(10)  # the first preview holds the worker while the rest wait
            return real(design, inputs)
        cfg = self.settings("welcomecard")

        async def preview(guild, person):
            return await self.cards._draw(guild.id, cfg, Scope(guild=guild, member=self.newcomer), person=person, avatar="", pool=budget.PREVIEWS,
                                          give_up=time.monotonic() + 20, max_bytes=8 << 20)
        with mock.patch.object(render, "render", held):
            mine = [asyncio.ensure_future(preview(self.guild, 1)) for _ in range(module.PREVIEW_WAITING)]
            await asyncio.sleep(0.3)
            theirs = asyncio.ensure_future(preview(other, 2))
            await asyncio.sleep(0.3)
            release.set()
            results = await asyncio.gather(*mine, theirs, return_exceptions=True)
        self.assertEqual(sum(isinstance(result, module._Busy) for result in results[:-1]), module.PREVIEW_WAITING - module.PREVIEW_PER_SERVER)
        self.assertIsInstance(results[-1], render.Picture, f"the other server's preview was turned away: {results[-1]!r}")
        self.assertEqual(order[:2], [1, 2], "the other server's preview is drawn next, ahead of the first server's that waited")
        previews = self.cards._previews
        self.assertEqual((previews.held, previews.by_server, previews.waiting, previews.running), (0, {}, [], 0), "every place and the worker go back")

    async def test_a_server_with_no_preview_in_a_full_line_gets_a_place(self):
        from captainguild.core.tagscript import budget
        await self.save(enabled=True)
        second, third = FakeGuild("Second Server"), FakeGuild("Third Server")
        self.bot.fake_guilds.extend([second, third])
        release = threading.Event()
        real = render.render
        order: list[int] = []

        def held(design, inputs):
            order.append(inputs.person)
            if len(order) == 1:
                release.wait(10)  # the first preview holds the worker while the rest wait
            return real(design, inputs)
        cfg = self.settings("welcomecard")

        def preview(guild, person):
            return self.cards._draw(guild.id, cfg, Scope(guild=guild, member=self.newcomer), person=person, avatar="", pool=budget.PREVIEWS,
                                    give_up=time.monotonic() + 20, max_bytes=8 << 20)
        with mock.patch.object(render, "render", held):
            full = [asyncio.ensure_future(preview(guild, person)) for guild, person in ((self.guild, 1), (second, 2)) for _ in range(module.PREVIEW_PER_SERVER)]
            await asyncio.sleep(0.3)
            self.assertEqual(self.cards._previews.held, module.PREVIEW_WAITING, "two servers at their share fill the line")
            first = asyncio.ensure_future(preview(third, 3))
            await asyncio.sleep(0.1)
            with self.assertRaises(module._Busy):
                await preview(third, 3)  # its second, while its first still waits and the line is full
            release.set()
            results = await asyncio.gather(*full, first, return_exceptions=True)
        self.assertIsInstance(results[-1], render.Picture, f"the third server's first preview was turned away: {results[-1]!r}")
        self.assertEqual(order[:2], [1, 3], "the server holding the fewest places is drawn next, ahead of the two that filled the line")
        previews = self.cards._previews
        self.assertEqual((previews.held, previews.by_server, previews.waiting, previews.running), (0, {}, [], 0))

    async def test_a_draw_that_waited_too_long_in_the_queue_is_not_started(self):
        await self.save(enabled=True)
        with self.watch_draws() as drawn:
            with self.assertRaises(render.RenderError):
                await self.cards._draw(self.guild.id, self.settings("welcomecard"), self.scope(), person=1, avatar="", pool="messages", give_up=time.monotonic() + 0.1, max_bytes=1_000_000)
        self.assertEqual(drawn.call_count, 0)

    async def test_the_renderer_is_told_how_long_it_has_and_how_heavy_the_file_may_be(self):
        await self.save(enabled=True)
        with self.watch_draws() as drawn:
            await self.picture()
        inputs = drawn.call_args.args[1]
        self.assertTrue(0 < inputs.seconds <= module.PICTURE_SECONDS)
        self.assertEqual(inputs.max_bytes, render.DEFAULT_MAX_BYTES)
        self.assertLessEqual(inputs.max_bytes, 20 * 1024 * 1024, "never above what a bot can upload")
        self.assertEqual(inputs.person, self.newcomer.id)

    async def test_past_the_servers_template_time_the_text_is_drawn_as_written(self):
        await self.save(enabled=True)

        async def spent(*args, **kwargs):
            raise OverBudget
        with mock.patch.object(module, "budgeted_render", spent), self.watch_draws() as drawn:
            self.assertIsNotNone(await self.picture())
        self.assertEqual(drawn.call_args.args[1].texts, {})

    async def test_the_stored_design_is_drawn_in_designer_mode(self):
        design = {"width": 320, "height": 200, "background": {"kind": "color", "color": "#ff0000"}, "layers": [{"type": "rect", "x": 0, "y": 0, "w": 50, "h": 50, "fill": {"kind": "color", "color": "#0000ff"}}]}
        await self.save(enabled=True, mode="design", design=design)
        image = Image.open(io.BytesIO((await self.picture())["data"])).convert("RGB")
        self.assertEqual((image.size, image.getpixel((10, 10)), image.getpixel((300, 180))), ((320, 200), (0, 0, 255), (255, 0, 0)))
        self.assertEqual(self.outside.asked, [])

    async def test_a_stored_design_this_version_cant_draw_is_noted(self):
        await self.save(enabled=True, mode="design")
        stored = self.bot.settings.get(self.guild.id, "welcomecard")
        with mock.patch.dict(stored, {"design": {"width": 5000, "height": 10}}):
            self.assertIsNone(await self.picture())
        self.assertIn("The stored design can't be drawn. The canvas can be 100 to 2,000 pixels on a side.", self.cards._problems[self.guild.id])

    async def test_a_note_from_the_renderer_reaches_the_page(self):
        await self.save(enabled=True)
        stored = self.bot.settings.get(self.guild.id, "welcomecard")
        with mock.patch.dict(stored, {"background": {"kind": "upload", "id": "0123456789abcdef"}}):
            self.assertIsNotNone(await self.picture())
        self.assertEqual(self.cards._problems[self.guild.id], "An uploaded picture this design names is gone, so the avatar's color stands in.")

    async def test_a_picture_layers_address_must_be_on_discords_image_servers(self):
        icon = "https://cdn.discordapp.com/icons/1/abc.png?size=128"
        self.outside.pictures[icon] = png(color=(0, 200, 0))

        def design(address: str) -> dict:
            return {"width": 300, "height": 150, "background": {"kind": "color", "color": "#000000"}, "layers": [{"type": "picture", "id": "pic", "x": 0, "y": 0, "w": 100, "h": 100, "source": {"kind": "address", "address": address}}]}
        await self.save(enabled=True, mode="design", design=design("https://cdn.discordapp.com/icons/1/abc.webp"))
        image = Image.open(io.BytesIO((await self.picture())["data"])).convert("RGB")
        self.assertEqual(image.getpixel((50, 50)), (0, 200, 0))
        self.assertEqual(self.outside.asked, [icon], "asked for as a PNG no larger than the layer needs")
        self.outside.asked.clear()
        for address in ("https://example.com/a.png", "http://cdn.discordapp.com/icons/1/abc.png", "https://cdn.discordapp.com.example.com/icons/1/a.png", "file:///etc/passwd", "{user}"):
            await self.save(design=design(address))
            self.assertIsNotNone(await self.picture(self.joiner("Next")))
        self.assertEqual(self.outside.asked, [], "none of those is ever fetched")
        self.assertIn("has no picture to show", self.cards._problems[self.guild.id])

    async def test_the_members_avatar_as_a_picture_layer(self):
        design = {"width": 300, "height": 150, "background": {"kind": "color", "color": "#000000"}, "layers": [{"type": "picture", "id": "pic", "x": 0, "y": 0, "w": 100, "h": 100, "source": {"kind": "address", "address": "{user(avatar)}"}}]}
        self.outside.pictures[f"https://cdn.discordapp.com/avatars/{self.newcomer.id}/abc.png?size=128"] = png(color=(200, 0, 200))
        await self.save(enabled=True, mode="design", design=design)
        self.assertEqual(Image.open(io.BytesIO((await self.picture())["data"])).convert("RGB").getpixel((50, 50)), (200, 0, 200))

    async def test_a_changed_setting_is_drawn_for_the_same_member_right_away(self):
        await self.save(enabled=True, background={"kind": "color", "color": "#ff0000"})
        first = await self.picture()
        await self.save(background={"kind": "color", "color": "#0000ff"})
        self.assertNotEqual((await self.picture())["data"], first["data"])


class CdnAddressTest(unittest.TestCase):
    def test_discords_own_pictures_are_asked_for_as_small_pngs(self):
        self.assertEqual(cdn_address("https://cdn.discordapp.com/avatars/1/abc.webp?size=4096", 256), "https://cdn.discordapp.com/avatars/1/abc.png?size=256")
        self.assertEqual(cdn_address("https://cdn.discordapp.com/avatars/1/a_abc.gif", 100), "https://cdn.discordapp.com/avatars/1/a_abc.png?size=128")
        self.assertEqual(cdn_address("https://cdn.discordapp.com/icons/1/abc.jpg", 5000), "https://cdn.discordapp.com/icons/1/abc.jpg?size=1024")
        self.assertEqual(cdn_address("https://cdn.discordapp.com/embed/avatars/3.png", 10), "https://cdn.discordapp.com/embed/avatars/3.png?size=64")
        self.assertEqual(cdn_address(" HTTPS://CDN.DISCORDAPP.COM/icons/1/abc.png ", 256), "https://cdn.discordapp.com/icons/1/abc.png?size=256", "however it is written, what is fetched is one plain form")

    def test_an_attachment_keeps_the_query_that_signs_it(self):
        self.assertEqual(cdn_address("https://media.discordapp.net/attachments/1/2/x.png?ex=66&is=77&hm=abc"), "https://media.discordapp.net/attachments/1/2/x.png?ex=66&is=77&hm=abc")
        self.assertEqual(cdn_address("https://cdn.discordapp.com/attachments/1/2/x.png?a=<script>"), "https://cdn.discordapp.com/attachments/1/2/x.png")

    def test_everything_else_is_refused(self):
        for address in ("http://cdn.discordapp.com/icons/1/a.png", "https://example.com/icons/1/a.png", "https://cdn.discordapp.com.example.com/a.png", "https://user:pw@cdn.discordapp.com/a.png",
                        "https://cdn.discordapp.com:8443/a.png", "https://cdn.discordapp.com/../a.png", "https://cdn.discordapp.com//a.png", "https://cdn.discordapp.com/a b.png", "https://cdn.discordapp.com",
                        "//cdn.discordapp.com/a.png", "cdn.discordapp.com/a.png", "javascript:alert(1)", "file:///etc/passwd", "https://[::1]/a.png", "https://cdn.discordapp.com:port/a.png", "", " ", None, 5,
                        "https://cdn.discordapp.com/" + "a" * 700):
            self.assertIsNone(cdn_address(address), repr(address))


class KeptTest(unittest.TestCase):
    def test_putting_an_item_costs_the_same_however_many_are_kept(self):
        kept = module._Kept(10**12)
        started = time.perf_counter()
        for index in range(20_000):
            kept.put(f"https://cdn.discordapp.com/avatars/{index:018d}/a.png", (0.0, None), 1)
        self.assertLess(time.perf_counter() - started, 0.5, "twenty thousand puts run in a moment, not in a time that grows with each one")
        kept.put("https://cdn.discordapp.com/avatars/000000000000000007/a.png", (1.0, "again"), 5)
        self.assertEqual((len(kept.items), kept.held), (20_000, 19_999 + 5), "putting a key again replaces it")
        self.assertEqual(next(reversed(kept.items)), "https://cdn.discordapp.com/avatars/000000000000000007/a.png", "and makes it the newest")

    def test_a_failed_fetch_is_counted_at_what_it_costs(self):
        cards = types.SimpleNamespace(outside=FakeOutside(), _fetched=module._Kept(module.FETCH_BYTES))
        address = "https://cdn.discordapp.com/avatars/1/gone.png?size=256"
        self.assertIsNone(asyncio.run(WelcomeCard._fetch(cards, address, keep=False)))
        self.assertEqual(cards._fetched.held, module.MISS_BYTES + len(address))
        self.assertGreaterEqual(module.MISS_BYTES + len(address), 300, "a kept miss with its address takes about 370 bytes")
        self.assertLess(module.FETCH_BYTES // (module.MISS_BYTES + 100), 100_000, "so the budget holds tens of thousands of misses, not hundreds of thousands")


class FetchingTest(unittest.IsolatedAsyncioTestCase):
    """The real Outside, with its session swapped for one that answers from a table, so what the bot sends can be seen."""

    async def fetched(self, location: str | None, final_host: str = "cdn.discordapp.com"):
        asked: list[str] = []

        class Response:
            def __init__(self, url: str, status: int, headers: dict, body: bytes = b"") -> None:
                self.url, self.status, self.headers, self._body = url, status, headers, body
                self.content = type("Content", (), {"iter_chunked": self._chunks})()

            async def _chunks(self, size: int):
                yield self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            closed = False

            def request(self, method, url, **kwargs):
                asked.append(str(url))
                if location is not None and str(url).startswith("https://cdn.discordapp.com/"):
                    return Response(str(url), 302, {"Location": location})
                return Response(str(url).replace("cdn.discordapp.com", final_host), 200, {}, png())

            async def close(self):
                pass
        real = Outside()
        real.offline = False  # the test fakes keep every Outside offline, and this one only reaches the Session above
        real._session = lambda stranger: Session()
        cards = types.SimpleNamespace(outside=real, _fetched=module._Kept(module.FETCH_BYTES))
        with mock.patch.object(outside_module, "check_public_link", lambda url: None):  # keeps the test off the network
            raw = await WelcomeCard._fetch(cards, module.cdn_address("https://cdn.discordapp.com/attachments/1/2/x.png"), keep=True)
        return raw, asked

    async def test_a_redirect_from_discords_image_server_to_another_site_is_never_followed(self):
        raw, asked = await self.fetched("https://collector.example.org/seen.png")
        self.assertIsNone(raw)
        self.assertEqual(asked, ["https://cdn.discordapp.com/attachments/1/2/x.png"], "no request left for the other site")

    async def test_a_redirect_between_discords_image_servers_is_followed(self):
        raw, asked = await self.fetched("https://media.discordapp.net/attachments/1/2/x.png")
        self.assertIsNotNone(raw)
        self.assertEqual(asked, ["https://cdn.discordapp.com/attachments/1/2/x.png", "https://media.discordapp.net/attachments/1/2/x.png"])

    async def test_an_answer_that_says_it_came_from_another_site_is_not_used(self):
        raw, _ = await self.fetched(None, final_host="collector.example.org")
        self.assertIsNone(raw, "the host check after the fetch still stands")


class AnimatedTest(Case):
    async def add(self, data: bytes, name: str = "Background") -> str:
        return (await self.cards.store_upload(self.guild.id, data, name))["id"]

    async def test_an_animated_upload_gives_a_gif_and_the_switch_makes_it_still(self):
        key = await self.add(moving_gif(200, 100, frames=5))
        await self.save(enabled=True, background={"kind": "upload", "id": key})
        moving = await self.picture()
        self.assertEqual(moving["filename"], "welcome.gif")
        image = Image.open(io.BytesIO(moving["data"]))
        self.assertEqual((image.format, image.n_frames, image.size), ("GIF", 5, (1000, 300)))
        await self.save(animate=False)
        still = await self.picture()
        self.assertEqual((still["filename"], Image.open(io.BytesIO(still["data"])).format), ("welcome.png", "PNG"))

    async def test_an_upload_is_read_from_the_database_once(self):
        key = await self.add(png(300, 100))
        await self.save(enabled=True, background={"kind": "upload", "id": key})
        await self.picture()
        with mock.patch.object(self.db, "one", side_effect=AssertionError("the picture was read from the database again")):
            await self.picture(self.joiner("Robin"))

    async def test_two_servers_with_an_upload_of_the_same_key_each_draw_their_own(self):
        # Keys are random, so this takes a copied database or a restored backup. Each server's picture still comes from its own row.
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        for guild, color in ((self.guild, (200, 30, 30)), (other, (30, 30, 200))):
            stored = uploads.prepare(png(300, 100, color=color))
            await self.db.run("INSERT INTO welcome_images (guild_id, key, name, kind, width, height, frames, size, image, preview, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (guild.id, "0123456789abcdef", "Same", stored.kind, stored.width, stored.height, stored.frames, len(stored.data),
                               base64.b64encode(stored.data).decode("ascii"), base64.b64encode(stored.preview).decode("ascii"), 0.0))
            self.cards._known.setdefault(guild.id, set()).add("0123456789abcdef")
            await self.bot.settings.save(guild.id, "welcomecard", {"enabled": True, "avatar": False, "background": {"kind": "upload", "id": "0123456789abcdef"}})
        seen = []
        for guild in (self.guild, other):
            member = FakeMember(guild, "Sam")
            picture = await self.cards.picture_for(member, "welcome", Scope(guild=guild, member=member))
            seen.append(Image.open(io.BytesIO(picture["data"])).convert("RGB").getpixel((5, 5)))
        self.assertTrue(near(seen[0], (200, 30, 30)) and near(seen[1], (30, 30, 200)), seen)

    async def test_another_servers_upload_is_out_of_reach(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = (await self.cards.store_upload(other.id, png(300, 100, color=(1, 2, 3)), "Theirs"))["id"]
        stored = self.bot.settings.get(self.guild.id, "welcomecard")
        with mock.patch.dict(stored, {"enabled": True, "avatar": False, "background": {"kind": "upload", "id": theirs}}):
            picture = await self.picture()
        self.assertNotEqual(Image.open(io.BytesIO(picture["data"])).convert("RGB").getpixel((5, 5)), (1, 2, 3))
        self.assertEqual(self.cards._problems[self.guild.id], "An uploaded picture this design names is gone, so the avatar's color stands in.")


class UploadPreparingTest(unittest.TestCase):
    def test_a_plain_picture_is_kept_as_a_jpeg_and_a_see_through_one_as_a_png(self):
        solid = uploads.prepare(png(400, 200))
        self.assertEqual((solid.kind, solid.width, solid.height, solid.frames), ("image/jpeg", 400, 200, 1))
        self.assertEqual(Image.open(io.BytesIO(solid.data)).format, "JPEG")
        clear = uploads.prepare(png(400, 200, color=(1, 2, 3, 100), mode="RGBA"))
        self.assertEqual((clear.kind, Image.open(io.BytesIO(clear.data)).mode), ("image/png", "RGBA"))

    def test_a_large_picture_is_scaled_to_the_largest_canvas(self):
        big = uploads.prepare(png(3000, 1500))
        self.assertEqual((big.width, big.height), (2000, 1000))

    def test_an_animated_gif_is_kept_exactly_as_it_came(self):
        source = moving_gif(128, 64, frames=4)
        kept = uploads.prepare(source)
        self.assertEqual((kept.data, kept.kind, kept.frames, kept.width, kept.height), (source, "image/gif", 4, 128, 64))

    def test_a_one_frame_gif_is_a_still_picture(self):
        self.assertEqual(uploads.prepare(moving_gif(128, 64, frames=1)).kind, "image/jpeg")

    def test_the_preview_is_a_small_jpeg(self):
        preview = Image.open(io.BytesIO(uploads.prepare(png(1200, 300)).preview))
        self.assertEqual((preview.format, preview.size), ("JPEG", (240, 60)))

    def test_a_sideways_photo_is_stored_upright(self):
        photo = Image.new("RGB", (200, 100), (10, 20, 30))
        exif = Image.Exif()
        exif[0x0112] = 6  # the camera was turned a quarter turn
        out = io.BytesIO()
        photo.save(out, format="JPEG", exif=exif)
        self.assertEqual((uploads.prepare(out.getvalue()).width, uploads.prepare(out.getvalue()).height), (100, 200))

    def test_what_is_not_a_usable_image_is_refused_with_a_sentence(self):
        webp = io.BytesIO()
        Image.new("RGB", (100, 100)).save(webp, format="WEBP")
        cases = ((b"", "That file is empty."), (b"hello there, not an image", "That file isn't an image the bot can read. Use a JPG, PNG or GIF."), (webp.getvalue(), "Use a JPG, PNG or GIF."),
                 (png(32, 32), "That image is 32x32 pixels. The smallest allowed is 64x64."), (png(5000, 100), "That image is 5000x100 pixels. The largest allowed is 4096x4096."))
        for data, sentence in cases:
            with self.assertRaises(Invalid) as refused:
                uploads.prepare(data)
            self.assertEqual(str(refused.exception), sentence)

    def test_a_gif_with_more_frames_than_can_be_animated_is_refused(self):
        with mock.patch.object(uploads, "MAX_ANIMATION_PIXELS", 128 * 64 * 3):
            self.assertEqual(uploads.prepare(moving_gif(128, 64, frames=3)).frames, 3)
            with self.assertRaises(Invalid) as refused:
                uploads.prepare(moving_gif(128, 64, frames=4))
        self.assertEqual(str(refused.exception), "That GIF has 4 frames of 128x64 pixels, which is more than the bot can animate. Use a shorter or smaller one.")

    def test_a_gif_of_more_frames_than_the_picture_reads_is_refused_with_both_counts(self):
        self.assertEqual(uploads.MAX_SOURCE_FRAMES, render.MAX_SOURCE_FRAMES)
        with mock.patch.object(uploads, "MAX_SOURCE_FRAMES", 5):
            self.assertEqual(uploads.prepare(moving_gif(128, 64, frames=5)).frames, 5)
            with self.assertRaises(Invalid) as refused:
                uploads.prepare(moving_gif(128, 64, frames=8))
        self.assertEqual(str(refused.exception), "That GIF has 8 frames, and a welcome picture reads at most 5. Use a shorter one.")

    def test_a_gif_that_breaks_partway_is_refused_at_upload(self):
        source = moving_gif(128, 64, frames=6)
        with self.assertRaises(Invalid) as refused:
            uploads.prepare(source[:len(source) * 2 // 3])
        self.assertEqual(str(refused.exception), uploads.UNREADABLE)

    def test_an_upload_keeps_no_comment_or_text_the_file_carried(self):
        from PIL import PngImagePlugin
        photo = io.BytesIO()
        Image.new("RGB", (200, 100), (10, 20, 30)).save(photo, format="JPEG", comment=b"GPS 51.5007N 0.1246W, taken by Sam at home")
        notes = PngImagePlugin.PngInfo()
        notes.add_text("comment", "taken by Sam at home")
        notes.add_text("Author", "Sam")
        for kind, mode in (("PNG", "RGB"), ("PNG", "RGBA")):
            out = io.BytesIO()
            Image.new(mode, (200, 100), (10, 20, 30, 200) if mode == "RGBA" else (10, 20, 30)).save(out, format=kind, pnginfo=notes)
            stored = Image.open(io.BytesIO(uploads.prepare(out.getvalue()).data))
            self.assertEqual({key for key in stored.info if key in ("comment", "Author")}, set(), f"{mode} PNG kept {stored.info}")
        stored = Image.open(io.BytesIO(uploads.prepare(photo.getvalue()).data))
        self.assertNotIn("comment", stored.info)

    def test_an_animated_gif_is_stored_without_what_its_file_carried(self):
        frames = [{"index": 1}, {"box": (8, 8, 40, 30), "index": 2, "disposal": 2, "before": b"!" + bytes([0xFE, 12]) + b"MARK-COMMENT" + bytes(1)}]
        plain = b"!" + bytes([0x01, 12]) + struct.pack("<HHHHBBBB", 0, 0, 10, 10, 8, 8, 1, 0) + bytes([10]) + b"MARK-PLAIN" + bytes(1)
        xmp = b"!" + bytes([0xFF, 11]) + b"XMP DataXMP" + bytes([8]) + b"MARK-APP" + bytes(1)
        sent = hand_gif((128, 64), frames, loop=3)
        sent = sent[:13 + 768] + plain + xmp + sent[13 + 768:]  # after the colors, as editors write them
        stored = uploads.prepare(sent)
        self.assertEqual((stored.kind, stored.frames), ("image/gif", 2))
        self.assertEqual([marker for marker in (b"MARK-COMMENT", b"MARK-PLAIN", b"MARK-APP") if marker in stored.data], [])
        before, after = gif.parse(sent, 10), gif.parse(stored.data, 10)
        self.assertEqual(after.loop, 3)
        self.assertEqual([frame.disposal for frame in after.frames], [frame.disposal for frame in before.frames])
        for (_, sent_canvas), (_, stored_canvas) in zip(gif.composed(before), gif.composed(after)):
            self.assertIsNone(ImageChops.difference(sent_canvas, stored_canvas).getbbox(), "every frame draws as it did")

    def test_an_animated_png_is_stored_as_a_png_without_what_its_file_carried(self):
        from PIL import ImageCms, PngImagePlugin
        exif = Image.Exif()
        exif[0x013B] = "MARK-ARTIST"
        exif.get_ifd(0x8825)[2] = (51.0, 30.0, 2.5)
        notes = PngImagePlugin.PngInfo()
        notes.add_text("Comment", "MARK-TEXT")
        made = [Image.new("RGB", (128, 64), (200, 40, 40)), Image.new("RGB", (128, 64), (40, 60, 200))]
        out = io.BytesIO()
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes() + b"MARK-ICC"
        made[0].save(out, format="PNG", save_all=True, append_images=made[1:], duration=100, pnginfo=notes, exif=exif.tobytes(), icc_profile=icc)
        stored = uploads.prepare(out.getvalue())
        self.assertEqual((stored.kind, stored.frames), ("image/png", 2))
        self.assertEqual([marker for marker in (b"MARK-ARTIST", b"MARK-TEXT", b"MARK-ICC") if marker in stored.data], [])
        with Image.open(io.BytesIO(stored.data)) as kept:
            self.assertEqual((kept.format, kept.n_frames, dict(kept.getexif())), ("PNG", 2, {}))
            kept.seek(1)
            self.assertEqual(kept.convert("RGB").getpixel((5, 5)), (40, 60, 200), "the frames draw as they did")

    def test_a_comment_too_long_to_save_is_dropped_and_a_save_that_fails_is_refused(self):
        from PIL import PngImagePlugin
        notes = PngImagePlugin.PngInfo()
        notes.add_text("comment", "x" * 70_000)
        out = io.BytesIO()
        Image.new("RGB", (200, 100), (10, 20, 30)).save(out, format="PNG", pnginfo=notes)
        self.assertEqual(uploads.prepare(out.getvalue()).kind, "image/jpeg")
        plain = png(200, 100)
        with mock.patch.object(Image.Image, "save", side_effect=ValueError("encoder error")), self.assertRaises(Invalid) as refused:
            uploads.prepare(plain)
        self.assertEqual(str(refused.exception), uploads.UNSAVABLE)

    def test_a_gif_of_more_blocks_than_the_reader_walks_is_refused_with_a_sentence(self):
        comments = (b"!" + bytes([0xFE, 0])) * 60
        data = hand_gif((128, 64), [{"index": 1}, {"box": (8, 8, 40, 30), "index": 2, "before": comments}])
        self.assertEqual(uploads.prepare(data).frames, 2)
        for crowded in (data, hand_gif((128, 64), [{"index": 1, "before": comments}])):  # stopped after a frame, or before the first
            with mock.patch.object(gif, "MAX_PIECES", 50), self.assertRaises(Invalid) as refused:
                uploads.prepare(crowded)
            self.assertEqual(str(refused.exception), uploads.CROWDED)
        with mock.patch.object(uploads, "MAX_SOURCE_FRAMES", 1), mock.patch.object(gif, "MAX_PIECES", 4000), self.assertRaises(Invalid) as refused:
            uploads.prepare(hand_gif((128, 64), [{"index": 1}] + [{"box": (index % 64, 0, 1, 1), "index": index % 5 + 1} for index in range(900)]))
        self.assertRegex(str(refused.exception), "^That GIF has more than [0-9,]+ frames, and a welcome picture reads at most 1. Use a shorter one.$")

    def test_a_gif_is_walked_once_and_opened_by_pillow_once(self):
        from PIL import GifImagePlugin
        real_walk, real_open = gif._walk, GifImagePlugin.GifImageFile._open
        walks, opens = [], []

        def walk(*args, **kwargs):
            walks.append(1)
            return real_walk(*args, **kwargs)

        def opened(image):
            opens.append(len(image.fp.getvalue()) if hasattr(image.fp, "getvalue") else 0)
            return real_open(image)
        for frames in ([{"index": 1}], [{"index": 1}, {"box": (8, 8, 40, 30), "index": 2}]):
            data = hand_gif((128, 64), frames)
            walks.clear()
            opens.clear()
            with mock.patch.object(gif, "_walk", walk), mock.patch.object(GifImagePlugin.GifImageFile, "_open", opened):
                uploads.prepare(data)
            self.assertEqual(len(walks), 1, f"{len(frames)} frames: the file's blocks are walked once")
            self.assertEqual(opens.count(len(data)), 1, f"{len(frames)} frames: Pillow opens the whole file once, for the image check")

    def test_a_gif_is_checked_by_decoding_its_frames_and_laying_only_the_first(self):
        frames = [{"index": 1}] + [{"box": (index * 4, 4, 30, 20), "index": 2 + index % 3} for index in range(5)] + [{"box": (500, 0, 10, 10), "index": 2}]
        data = hand_gif((128, 64), frames)
        real = gif._read
        laid = []

        def counted(frame):
            laid.append(frame)
            return real(frame)
        with mock.patch.object(gif, "_read", counted):
            self.assertEqual(uploads.prepare(data).frames, 7)
        self.assertEqual(len(laid), 1, "only the first frame is laid, for the preview")
        broken = frames[:3] + [{"box": (16, 16, 16, 16), "data": lzw_codes([256, 3, 400, 257])}] + frames[3:]
        with self.assertRaises(Invalid) as refused:
            uploads.prepare(hand_gif((128, 64), broken))
        self.assertEqual(str(refused.exception), uploads.UNREADABLE, "every frame that shows is still decoded once")
        outside = frames + [{"box": (500, 0, 16, 16), "data": lzw_codes([256, 3, 400, 257])}]
        self.assertEqual(uploads.prepare(hand_gif((128, 64), outside)).frames, 8, "a frame wholly outside the picture is never read, here or when drawn")

    def test_a_gif_with_a_frame_whose_code_size_is_under_two_is_refused(self):
        with self.assertRaises(Invalid) as refused:
            uploads.prepare(hand_gif((128, 64), [{"index": 1}, {"box": (8, 8, 40, 30), "code": 1, "data": lzw_codes([2, 1] * 1200 + [3], width=2)}]))
        self.assertEqual(str(refused.exception), uploads.UNREADABLE)

    def test_a_gif_without_colors_is_kept(self):
        self.assertEqual(uploads.prepare(hand_gif((128, 64), [{"index": 1}], colors=None)).kind, "image/jpeg", "a still one, drawn black")
        moving = hand_gif((128, 64), [{"index": 1, "local": GIF_COLORS}, {"box": (8, 8, 40, 30), "index": 2}], colors=None)
        self.assertEqual(uploads.prepare(moving).frames, 2)

    def test_a_gif_with_a_frame_that_cannot_be_decoded_is_refused_at_upload(self):
        broken = lzw_codes([256, 3, 400, 257])  # the second code names a table entry that doesn't exist yet
        frames = [{"index": 1}, {"box": (0, 0, 16, 16), "index": 2}, {"box": (16, 16, 16, 16), "data": broken}]
        with self.assertRaises(Invalid) as refused:
            uploads.prepare(hand_gif((128, 64), frames))
        self.assertEqual(str(refused.exception), uploads.UNREADABLE)

    def test_a_gif_preview_shows_the_first_frame_as_a_browser_does(self):
        frames = [{"box": (32, 16, 64, 32), "index": 5}, {"box": (0, 0, 16, 16), "index": 2}]
        preview = Image.open(io.BytesIO(uploads.prepare(hand_gif((128, 64), frames, colors=[RED] + GIF_COLORS[1:])).preview)).convert("RGB")
        self.assertTrue(near(preview.getpixel((5, 5)), uploads.BACKING, 12), f"see-through around the first frame, not red: {preview.getpixel((5, 5))}")
        still = uploads.prepare(hand_gif((128, 64), frames[:1], colors=[RED] + GIF_COLORS[1:]))
        self.assertEqual((still.kind, Image.open(io.BytesIO(still.data)).convert("RGBA").getpixel((5, 5))[3]), ("image/png", 0), "a GIF of one frame too")

    def test_a_gif_is_counted_and_measured_as_a_browser_shows_it(self):
        frames = [{"index": 1}, {"box": (0, 0, 16, 16), "index": 2}, {"box": (16, 16, 16, 16), "index": 3, "before": bytes(1)}, {"box": (32, 32, 16, 16), "index": 5}]
        self.assertEqual(uploads.prepare(hand_gif((128, 64), frames)).frames, 2, "a stray byte ends the file")
        cut = hand_gif((128, 64), [frames[0], frames[1], frames[3]], tail=b"!" + bytes([0xFE, 20]) + b"cut short")
        self.assertEqual(uploads.prepare(cut).frames, 3, "a file cut inside a block after its last frame has lost no frame")
        grown = hand_gif((128, 64), [{"box": (64, 0, 128, 64), "index": 1}, {"box": (0, 0, 16, 16), "index": 2}])
        self.assertEqual((uploads.prepare(grown).width, uploads.prepare(grown).height), (192, 64), "the first frame runs past the edge and grows the picture")
        with mock.patch.object(uploads, "MAX_ANIMATION_PIXELS", 2 * 128 * 64), self.assertRaises(Invalid) as refused:
            uploads.prepare(grown)
        self.assertEqual(str(refused.exception), "That GIF has 2 frames of 192x64 pixels, which is more than the bot can animate. Use a shorter or smaller one.")


class RoutesTest(Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.admin.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{self.admin.id}/abc.png?size=1024")
        self.outside.pictures[avatar_of(self.admin)] = face_png()
        self.site = await self.client()

    async def preview(self, **draft):
        return await self.site.post(self.path("/welcomecard/preview"), json={"draft": draft})

    @staticmethod
    def shown(answer: dict) -> Image.Image:
        head, _, body = answer["image"].partition(",")
        assert head.startswith("data:image/") and head.endswith(";base64"), head
        return Image.open(io.BytesIO(base64.b64decode(body)))

    async def test_the_catalog_lists_what_the_pickers_offer(self):
        data = await (await self.site.get(self.path("/welcomecard/catalog"))).json()
        self.assertEqual([row["key"] for row in data["layouts"]], list(layouts.KEYS))
        self.assertEqual([row["key"] for row in data["fonts"]], list(fonts.KEYS))
        self.assertGreaterEqual(len(data["stock"]), 40)
        self.assertEqual(data["categories"], list(backgrounds.CATEGORIES))
        self.assertGreaterEqual(len(data["stripes"]), 8)
        self.assertEqual((data["positions"][0], len(data["positions"])), ("auto", 10), "the layout's own position comes first, then the nine")
        self.assertEqual(data["shapes"], ["circle", "rounded", "square", "hexagon"])
        self.assertEqual((data["limits"]["side"], data["limits"]["layers"], data["limits"]["uploads"]), ([100, 2000], 40, module.MAX_UPLOADS))
        self.assertEqual(data["defaults"]["text"], layouts.DEFAULT_TEXT)

    async def test_the_small_pictures_for_the_pickers(self):
        for tail, size in (("/welcomecard/stock/aurora", backgrounds.THUMB), ("/welcomecard/fonts/lora", (300, 56)), ("/welcomecard/layouts/tall", (320, 220))):
            response = await self.site.get(self.path(tail))
            self.assertEqual((response.status, response.content_type), (200, "image/png"), tail)
            self.assertEqual(Image.open(io.BytesIO(await response.read())).size, size, tail)
            self.assertIn("max-age", response.headers["Cache-Control"])
        for tail in ("/welcomecard/stock/nope", "/welcomecard/fonts/nope", "/welcomecard/layouts/nope"):
            self.assertEqual((await self.site.get(self.path(tail))).status, 404, tail)

    async def test_every_font_and_layout_and_stock_background_has_its_small_picture(self):
        for key in fonts.KEYS:
            self.assertEqual((await self.site.get(self.path(f"/welcomecard/fonts/{key}"))).status, 200, key)
        for key in layouts.KEYS:
            self.assertEqual((await self.site.get(self.path(f"/welcomecard/layouts/{key}"))).status, 200, key)
        self.assertEqual((await self.site.get(self.path("/welcomecard/stock/hills"))).status, 200)

    async def test_the_preview_draws_the_saved_settings_for_whoever_is_looking(self):
        with self.watch_draws() as drawn:
            response = await self.preview()
        self.assertEqual(response.status, 200, await response.text())
        answer = await response.json()
        image = self.shown(answer)
        self.assertEqual((answer["kind"], answer["width"], answer["height"], answer["frames"], image.format, image.size), ("png", 1000, 300, 1, "PNG", (1000, 300)))
        self.assertEqual(answer["canvas"], {"width": 1000, "height": 300, "frame": 0})
        self.assertEqual(answer["bytes"], len(base64.b64decode(answer["image"].partition(",")[2])))
        self.assertIn("text", answer["sizes"])
        inputs = drawn.call_args.args[1]
        self.assertIn(self.admin.name, inputs.texts["text"].text)
        self.assertEqual(inputs.person, self.admin.id)
        self.assertEqual(self.outside.asked, [avatar_of(self.admin)])

    async def test_the_preview_works_while_pictures_are_switched_off(self):
        self.assertFalse(self.settings("welcomecard")["enabled"])
        self.assertEqual((await self.preview()).status, 200)

    async def test_a_draft_is_drawn_and_nothing_is_saved(self):
        before = dict(self.settings("welcomecard"))
        response = await self.preview(layout="tall", frame=True, frame_width=10, background={"kind": "color", "color": "#00ff00"}, text="Draft")
        answer = await response.json()
        self.assertEqual((answer["width"], answer["height"], answer["canvas"]), (770, 536, {"width": 750, "height": 516, "frame": 10}))
        self.assertEqual(self.shown(answer).convert("RGB").getpixel((20, 20)), (0, 255, 0))
        self.assertEqual(self.settings("welcomecard"), before)
        self.assertEqual(await self.bot.activity_log.recent(self.guild.id), [])

    async def test_a_drafted_design_is_drawn_in_designer_mode(self):
        design = {"width": 300, "height": 200, "background": {"kind": "color", "color": "#0000ff"}, "layers": []}
        answer = await (await self.preview(mode="design", design=design)).json()
        self.assertEqual((answer["width"], answer["height"], self.shown(answer).convert("RGB").getpixel((5, 5))), (300, 200, (0, 0, 255)))

    async def test_a_draft_the_settings_would_refuse_is_refused_the_same_way(self):
        response = await self.preview(layout="sideways")
        self.assertEqual(response.status, 400)
        self.assertIn("layout", (await response.json())["fields"])
        response = await self.preview(mode="design", design={"width": 3000, "height": 300})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"]["design"], "The canvas can be 100 to 2,000 pixels on a side. Its width is 3,000.")
        response = await self.site.post(self.path("/welcomecard/preview"), json={"draft": ["not", "fields"]})
        self.assertEqual(response.status, 400)
        self.assertEqual((await self.preview(unknown=1)).status, 400)

    async def test_an_animated_preview_is_a_gif(self):
        key = (await self.cards.store_upload(self.guild.id, moving_gif(200, 100, frames=4), "Moving"))["id"]
        answer = await (await self.preview(background={"kind": "upload", "id": key})).json()
        self.assertEqual((answer["kind"], answer["frames"]), ("gif", 4))
        self.assertTrue(answer["image"].startswith("data:image/gif;base64,"))
        self.assertEqual(self.shown(answer).n_frames, 4)
        self.assertEqual((await (await self.preview(background={"kind": "upload", "id": key}, animate=False)).json())["kind"], "png")

    async def test_a_preview_reports_what_did_not_go_as_designed(self):
        answer = await (await self.preview(mode="design", design={"width": 300, "height": 200, "layers": [{"type": "text", "name": "Title", "x": 0, "y": 0, "w": 40, "h": 12, "text": "word " * 80}]})).json()
        self.assertEqual(answer["notes"], ["The layer Title holds more text than its box has room for, so the end is cut."])

    async def test_a_preview_that_cant_be_drawn_says_why(self):
        with mock.patch.object(render, "render", side_effect=render.RenderError("The picture took too long to draw. Use fewer or smaller layers.")):
            response = await self.preview()
        self.assertEqual((response.status, (await response.json())["error"]), (422, "The picture took too long to draw. Use fewer or smaller layers."))
        # A full line, in which this server already has a place. One with none would get a place even so.
        with mock.patch.object(self.cards._previews, "held", module.PREVIEW_WAITING), mock.patch.object(self.cards._previews, "by_server", {self.guild.id: 1}):
            response = await self.preview()
        self.assertEqual((response.status, (await response.json())["error"]), (503, "The bot is busy drawing welcome pictures. Try again in a moment."))

    async def test_the_previews_answer_is_encoded_off_the_event_loop(self):
        on_the_loop = []
        real = module.base64.b64encode

        def watched(data, *args):
            on_the_loop.append(threading.current_thread() is threading.main_thread())
            return real(data, *args)
        with mock.patch.object(module.base64, "b64encode", watched):
            response = await self.preview()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.shown(await response.json()))
        self.assertEqual(on_the_loop, [False], "the picture is turned into text on a worker thread")

    async def test_previews_never_take_the_places_or_the_workers_joins_draw_with(self):
        await self.save(enabled=True)
        release = threading.Event()
        real = render.render
        busy = "The bot is busy drawing welcome pictures. Try again in a moment."

        def held(design, inputs):
            if inputs.person == self.admin.id:  # a preview, drawn for the person looking at the page
                release.wait(8)
            return real(design, inputs)
        with mock.patch.object(render, "render", held):
            previews = [asyncio.ensure_future(self.preview(text=f"draft {index}")) for index in range(module.MAX_WAITING + 3)]
            kept = module.PREVIEW_PER_SERVER
            try:
                for _ in range(200):  # until every preview has its place in the line or has been turned away
                    if self.cards._previews.held == kept and sum(preview.done() for preview in previews) == len(previews) - kept:
                        break
                    await asyncio.sleep(0.05)
                turned_away = [preview for preview in previews if preview.done()]
                self.assertEqual(len(turned_away), len(previews) - kept, "the server's share of the previews' line is full")
                for preview in turned_away:
                    self.assertEqual((preview.result().status, (await preview.result().json())["error"]), (503, busy))
                command = self.context(author=self.admin)
                command.interaction = object()
                await self.bot.get_command("greetings image_preview").callback(self.cards, command)
                self.assertEqual(command.fails, [busy], "the preview command waits in the previews' line too")
                started = time.monotonic()
                joins = await asyncio.gather(*(self.picture(self.joiner(f"newcomer{index}")) for index in range(3)))
                self.assertLess(time.monotonic() - started, 5.0, "joins draw on their own workers while previews hold theirs")
            finally:
                release.set()
                await asyncio.gather(*previews, return_exceptions=True)
        self.assertNotIn(None, joins)
        self.assertEqual(self.cards.problems(self.guild.id), [], "no join went without its picture, and the page says nothing of a burst")

    async def test_a_design_the_drawing_libraries_refuse_gets_a_sentence_and_no_server_error(self):
        made = {"width": 400, "height": 200, "layers": [{"type": "rect", "x": 10, "y": 10, "w": 10, "h": 100, "fill": {"kind": "stripes", "direction": "vertical", "colors": ["#ff0000"] * 24}}]}
        response = await self.preview(mode="design", design=made)
        self.assertEqual(response.status, 200, "a design that saves is drawn")
        with mock.patch.object(render._Draw, "overlay", side_effect=ValueError("x1 must be greater than or equal to x0")):
            response = await self.preview(mode="design", design=made)
        self.assertEqual((response.status, (await response.json())["error"]), (422, render.UNDRAWABLE))

    async def test_a_preview_uses_the_values_greetings_builds_when_it_can(self):
        greetings = self.cog(Greetings)
        asked = []

        async def scope_for(member, channel=None, preview=False):
            asked.append((member, channel, preview))
            return Scope(guild=member.guild, member=member, extra={"joined": "64,097"})
        with mock.patch.object(greetings, "scope_for", scope_for, create=True), self.watch_draws() as drawn:
            await self.preview(text="Member {joined}")
        self.assertEqual(asked, [(self.admin, None, True)])
        self.assertEqual(drawn.call_args.args[1].texts["text"].text, "Member 64,097")

    async def test_a_preview_still_draws_when_greetings_cant_build_the_values(self):
        greetings = self.cog(Greetings)

        async def scope_for(member, channel=None, preview=False):
            raise RuntimeError("not today")
        with mock.patch.object(greetings, "scope_for", scope_for, create=True), self.assertLogs("captainguild.modules.welcomecard", "WARNING"):
            self.assertEqual((await self.preview()).status, 200)

    async def test_turning_a_layout_into_a_design(self):
        before = dict(self.settings("welcomecard"))
        response = await self.site.post(self.path("/welcomecard/design"), json={"draft": {"layout": "badge", "text": "Hi {user}", "ring_color": "#ff0000"}})
        self.assertEqual(response.status, 200)
        design = (await response.json())["design"]
        self.assertEqual(design, layouts.build({**layouts.DEFAULTS, "layout": "badge", "text": "Hi {user}", "ring_color": "#ff0000"}))
        self.assertEqual([layer["id"] for layer in design["layers"]], ["panel", "avatar", "text"])
        self.assertEqual(self.settings("welcomecard"), before)
        saved = await self.site.put(self.path("/settings/welcomecard"), json={"mode": "design", "design": design})
        self.assertEqual(saved.status, 200, await saved.text())
        self.assertEqual(self.settings("welcomecard")["design"], design)

    # ---- uploads ----

    async def upload(self, data: bytes, name: str | None = "Banner"):
        return await self.site.post(self.path("/welcomecard/uploads"), data=upload_form(data, name))

    async def test_upload_list_fetch_and_remove(self):
        response = await self.upload(png(600, 200, color=(9, 99, 199)), "  My   banner ")
        self.assertEqual(response.status, 200, await response.text())
        row = await response.json()
        self.assertEqual((row["name"], row["width"], row["height"], row["frames"], row["animated"]), ("My banner", 600, 200, 1, False))
        self.assertRegex(row["id"], r"^[0-9a-f]{16}$")
        listed = await (await self.site.get(self.path("/welcomecard/uploads"))).json()
        self.assertEqual(([item["id"] for item in listed["uploads"]], listed["most"], listed["room"], listed["used"]), ([row["id"]], module.MAX_UPLOADS, module.MAX_UPLOAD_TOTAL, row["size"]))
        whole = await self.site.get(row["url"])
        self.assertEqual((whole.status, whole.content_type, whole.headers["X-Content-Type-Options"]), (200, "image/jpeg", "nosniff"))
        self.assertTrue(all(abs(a - b) < 6 for a, b in zip(Image.open(io.BytesIO(await whole.read())).convert("RGB").getpixel((5, 5)), (9, 99, 199))))
        small = await self.site.get(row["preview"])
        self.assertEqual((small.status, small.content_type, Image.open(io.BytesIO(await small.read())).size), (200, "image/jpeg", (240, 80)))
        removed = await self.site.delete(self.path(f"/welcomecard/uploads/{row['id']}"))
        self.assertEqual(removed.status, 200)
        self.assertEqual((await (await self.site.get(self.path("/welcomecard/uploads"))).json())["uploads"], [])
        self.assertEqual((await self.site.get(row["url"])).status, 404)
        self.assertEqual([entry["action"] for entry in await self.bot.activity_log.recent(self.guild.id)], ["welcomecard.upload_remove", "welcomecard.upload"])

    async def test_a_served_upload_tells_the_browser_not_to_guess_its_type_by_itself(self):
        # The site adds the header to every answer too. This reads the route's own answer, so the route keeps it with the site's help gone.
        row = await (await self.upload(png(300, 100))).json()
        for preview in ("", "1"):
            req = types.SimpleNamespace(guild_id=self.guild.id, query={"preview": preview}, param=lambda name: row["id"])
            answer = await self.cards._upload_file_endpoint(req)
            self.assertEqual(answer.headers.get("X-Content-Type-Options"), "nosniff")

    async def test_an_uploaded_picture_can_be_the_background(self):
        row = await (await self.upload(png(600, 200, color=(250, 10, 10)))).json()
        saved = await self.site.put(self.path("/settings/welcomecard"), json={"background": {"kind": "upload", "id": row["id"]}, "avatar": False, "text": ""})
        self.assertEqual(saved.status, 200, await saved.text())
        pixel = self.shown(await (await self.preview()).json()).convert("RGB").getpixel((500, 150))
        self.assertTrue(pixel[0] > 240 and pixel[1] < 20 and pixel[2] < 20, pixel)

    async def test_a_picture_that_was_never_uploaded_cant_be_saved_as_the_background(self):
        response = await self.site.put(self.path("/settings/welcomecard"), json={"background": {"kind": "upload", "id": "0123456789abcdef"}})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["fields"], {"background": "That uploaded picture is gone. Pick another one."})
        design = {"width": 300, "height": 200, "layers": [{"type": "rect", "fill": {"kind": "upload", "id": "0123456789abcdef"}}]}
        response = await self.site.put(self.path("/settings/welcomecard"), json={"design": design})
        self.assertEqual((response.status, (await response.json())["fields"]), (400, {"design": "That uploaded picture is gone. Pick another one."}))

    async def test_another_servers_upload_cant_be_named_fetched_or_removed(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        theirs = (await self.cards.store_upload(other.id, png(300, 100), "Theirs"))["id"]
        response = await self.site.put(self.path("/settings/welcomecard"), json={"background": {"kind": "upload", "id": theirs}})
        self.assertEqual(response.status, 400)
        self.assertEqual((await self.site.get(self.path(f"/welcomecard/uploads/{theirs}"))).status, 404)
        self.assertEqual((await self.site.delete(self.path(f"/welcomecard/uploads/{theirs}"))).status, 404)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM welcome_images WHERE guild_id = ?", (other.id,)), 1)

    async def test_a_picture_in_use_cant_be_removed(self):
        row = await (await self.upload(png(600, 200))).json()
        await self.save(background={"kind": "upload", "id": row["id"]})
        response = await self.site.delete(self.path(f"/welcomecard/uploads/{row['id']}"))
        self.assertEqual(response.status, 409)
        self.assertIn("The welcome picture uses that one.", (await response.json())["error"])
        await self.save(background={"kind": "avatar"}, design={"width": 300, "height": 200, "layers": [{"type": "picture", "source": {"kind": "upload", "id": row["id"]}}]})
        self.assertEqual((await self.site.delete(self.path(f"/welcomecard/uploads/{row['id']}"))).status, 409, "the stored design counts even while the layouts are in use")
        await self.save(design={"width": 300, "height": 200, "layers": []})
        self.assertEqual((await self.site.delete(self.path(f"/welcomecard/uploads/{row['id']}"))).status, 200)

    async def test_a_removed_picture_no_longer_blocks_other_changes(self):
        row = await (await self.upload(png(600, 200))).json()
        await self.save(background={"kind": "upload", "id": row["id"]})
        await self.db.run("DELETE FROM welcome_images WHERE guild_id = ?", (self.guild.id,))
        await self.cards.on_guild_restored(self.guild.id)
        response = await self.site.put(self.path("/settings/welcomecard"), json={"text": "Still saves", "background": {"kind": "upload", "id": row["id"]}})
        self.assertEqual(response.status, 200, await response.text())

    async def test_what_is_not_a_usable_image_is_refused(self):
        for data, sentence in ((b"not an image at all", "That file isn't an image the bot can read. Use a JPG, PNG or GIF."), (png(20, 20), "That image is 20x20 pixels. The smallest allowed is 64x64.")):
            response = await self.upload(data)
            self.assertEqual((response.status, (await response.json())["error"]), (400, sentence))
        response = await self.site.post(self.path("/welcomecard/uploads"), json={})
        self.assertEqual((response.status, (await response.json())["error"]), (400, "Send this as a file upload."))
        body = aiohttp.FormData()
        body.add_field("name", "No file", content_type="text/plain")
        body.add_field("other", "x", content_type="text/plain")
        response = await self.site.post(self.path("/welcomecard/uploads"), data=body)
        self.assertEqual((response.status, (await response.json())["error"]), (400, "Pick an image first."))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM welcome_images"), 0)

    async def test_a_file_over_the_limit_is_refused(self):
        response = await self.site.post(self.path("/welcomecard/uploads"), data=upload_form(png(64, 64) + b"\x00" * images.MAX_BYTES))
        self.assertEqual((response.status, (await response.json())["error"]), (400, "That file is too large. The limit is 8 MB."))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM welcome_images"), 0)

    async def test_a_server_keeps_only_so_many_pictures(self):
        with mock.patch.object(module, "MAX_UPLOADS", 2):
            self.assertEqual((await self.upload(png(100, 100))).status, 200)
            self.assertEqual((await self.upload(png(100, 100))).status, 200)
            response = await self.upload(png(100, 100))
        self.assertEqual((response.status, (await response.json())["error"]), (409, "A server can keep 2 uploaded pictures. Remove one first."))
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM welcome_images"), 2)

    async def test_a_servers_pictures_have_a_weight_limit_together(self):
        first = await (await self.upload(png(300, 300))).json()
        with mock.patch.object(module, "MAX_UPLOAD_TOTAL", first["size"] + 10):
            response = await self.upload(png(300, 300))
        self.assertEqual(response.status, 409)
        self.assertIn("doesn't fit", (await response.json())["error"])

    async def test_uploads_sent_at_the_same_moment_stay_within_the_servers_limits(self):
        with mock.patch.object(module, "MAX_UPLOADS", 3):
            answers = await asyncio.gather(*(self.upload(png(200 + index, 100)) for index in range(12)))
        statuses = sorted(answer.status for answer in answers)
        self.assertEqual(statuses, [200] * 3 + [409] * 9)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM welcome_images WHERE guild_id = ?", (self.guild.id,)), 3)
        await self.db.run("DELETE FROM welcome_images")
        self.cards._known.clear()
        weight = (await (await self.upload(png(300, 300))).json())["size"]
        await self.db.run("DELETE FROM welcome_images")
        self.cards._known.clear()
        with mock.patch.object(module, "MAX_UPLOAD_TOTAL", 2 * weight + 10):
            answers = await asyncio.gather(*(self.upload(png(300, 300)) for _ in range(8)))
        self.assertEqual(sorted(answer.status for answer in answers), [200] * 2 + [409] * 6)
        self.assertLessEqual(await self.db.scalar("SELECT COALESCE(SUM(size), 0) FROM welcome_images"), 2 * weight + 10)

    async def test_an_animated_upload_is_listed_as_animated(self):
        row = await (await self.upload(moving_gif(120, 64, frames=5))).json()
        self.assertEqual((row["frames"], row["animated"]), (5, True))
        whole = await self.site.get(row["url"])
        self.assertEqual((whole.content_type, Image.open(io.BytesIO(await whole.read())).n_frames), ("image/gif", 5))

    async def test_a_stored_row_of_an_odd_type_is_never_served(self):
        await self.db.run("INSERT INTO welcome_images (guild_id, key, name, kind, width, height, frames, size, image, preview, created_at) VALUES (?, '0123456789abcdef', 'x', 'text/html', 1, 1, 1, 1, ?, ?, 1)",
                          (self.guild.id, base64.b64encode(b"<script>alert(1)</script>").decode(), "not base64 at all!"))
        self.assertEqual((await self.site.get(self.path("/welcomecard/uploads/0123456789abcdef"))).status, 404)
        await self.db.run("UPDATE welcome_images SET kind = 'image/png'")
        self.assertEqual((await self.site.get(self.path("/welcomecard/uploads/0123456789abcdef?preview=1"))).status, 404)
        for odd in ("nope", "0123", "0123456789ABCDEF", "..%2F..%2Fsecret"):
            self.assertEqual((await self.site.get(self.path(f"/welcomecard/uploads/{odd}"))).status, 404, odd)

    async def test_the_problem_note_is_served_to_the_page(self):
        self.assertEqual(await (await self.site.get(self.path("/welcomecard/problems"))).json(), [])
        self.cards._note(self.guild.id, "The last welcome picture took too long to draw, so the welcome went out without it.")
        self.cards._note_skipped(self.guild.id)
        self.assertEqual(await (await self.site.get(self.path("/welcomecard/problems"))).json(), [
            "The last welcome picture took too long to draw, so the welcome went out without it.",
            "1 welcome in the last ten minutes went out with no picture, because more people joined at once than the bot draws pictures for.",
        ])

    # ---- settings ----

    async def test_every_simple_setting_saves_through_the_settings_route(self):
        form = {"enabled": True, "dm_image": True, "in_embed": False, "animate": False, "mode": "layout", "layout": "split", "background": {"kind": "stripes", "direction": "vertical", "colors": ["#FFF", "#000000"]},
                "text": "Line one\r\nLine two", "text_position": "bottom_right", "text_color": "#ABCDEF", "outline_color": "rgba(0, 0, 0, 0.5)", "outline_width": 6, "font": "anton", "avatar": False,
                "avatar_shape": "hexagon", "ring_color": 0xFF8800, "ring_width": 12, "frame": True, "frame_color": "#123", "frame_width": 24}
        response = await self.site.put(self.path("/settings/welcomecard"), json=form)
        self.assertEqual(response.status, 200, await response.text())
        saved = self.settings("welcomecard")
        self.assertEqual((saved["text"], saved["text_color"], saved["outline_color"], saved["ring_color"], saved["frame_color"]), ("Line one\nLine two", "#abcdef", "#00000080", "#ff8800", "#112233"))
        self.assertEqual(saved["background"], {"kind": "stripes", "direction": "vertical", "colors": ["#ffffff", "#000000"]})
        self.assertEqual({key: saved[key] for key in form if key not in ("text", "text_color", "outline_color", "ring_color", "frame_color", "background")},
                         {key: value for key, value in form.items() if key not in ("text", "text_color", "outline_color", "ring_color", "frame_color", "background")})

    async def test_settings_out_of_range_are_refused_field_by_field(self):
        cases = {"layout": "diagonal", "font": "comic", "text_position": "middle", "avatar_shape": "star", "mode": "both", "outline_width": 33, "ring_width": 65, "frame_width": 0, "text_color": "red",
                 "background": {"kind": "stripes", "colors": []}, "text": "x" * 1001, "design": {"layers": [{"type": "rect"}] * 41}, "enabled": "yes"}
        for field, value in cases.items():
            response = await self.site.put(self.path("/settings/welcomecard"), json={field: value})
            self.assertEqual(response.status, 400, field)
            self.assertIn(field, (await response.json())["fields"], field)
        self.assertEqual((await (await self.site.put(self.path("/settings/welcomecard"), json={"design": {"layers": [{"type": "rect"}] * 41}})).json())["fields"]["design"], "A design can hold at most 40 layers. This one has 41.")
        self.assertEqual((await (await self.site.put(self.path("/settings/welcomecard"), json={"background": {"kind": "stripes", "colors": []}})).json())["fields"]["background"], "Stripes need at least 1 color.")

    async def test_the_settings_start_as_the_layouts_start(self):
        saved = self.settings("welcomecard")
        self.assertEqual({key: saved[key] for key in layouts.DEFAULTS}, layouts.DEFAULTS)
        self.assertEqual((saved["enabled"], saved["dm_image"], saved["in_embed"], saved["animate"], saved["mode"]), (False, False, True, True, "layout"))
        self.assertEqual(saved["design"], layouts.build(layouts.DEFAULTS))
        self.assertEqual(model.clean_design(saved["design"], fonts.KEYS), saved["design"])


class CommandsTest(Case):
    def command(self, name: str):
        return self.bot.get_command(f"greetings {name}")

    async def run_command(self, name: str, *args, author=None, ctx=None):
        ctx = ctx or self.context(author=author or self.admin)
        command = self.command(name)
        await command.callback(self.cards, ctx, *args)
        return ctx

    async def test_three_subcommands_join_the_greetings_group(self):
        added = sorted(command.name for command in self.bot.get_command("greetings").commands if command.cog is self.cards)
        self.assertEqual(added, ["image", "image_dm", "image_preview"])
        for name in added:
            self.assertTrue(name.startswith("image"))
            self.assertEqual(required_permissions(self.command(name)), ("manage_guild",), name)
            self.assertIsNotNone(self.command(name).app_command, f"{name} is a slash command too")
            self.assertLessEqual(len(self.command(name).description), 100)

    async def test_the_group_stays_within_discords_limit(self):
        self.assertLessEqual(len(self.bot.get_command("greetings").app_command.commands), 25)

    async def test_image_switches_pictures_on_and_off(self):
        ctx = await self.run_command("image", "on")
        self.assertTrue(self.settings("welcomecard")["enabled"])
        self.assertIn("Welcome pictures are on.", ctx.oks[0])
        self.assertIn(f"/dashboard/{self.guild.id}/welcomecard", ctx.oks[0])
        ctx = await self.run_command("image", "off")
        self.assertFalse(self.settings("welcomecard")["enabled"])
        self.assertEqual(ctx.oks, ["Welcome pictures are off."])
        actions = [(entry["action"], entry["user_id"]) for entry in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions, [("welcomecard.enabled", self.admin.id), ("welcomecard.enabled", self.admin.id)])

    async def test_image_dm_switches_the_picture_in_the_join_dm(self):
        ctx = await self.run_command("image_dm", "on")
        self.assertTrue(self.settings("welcomecard")["dm_image"])
        self.assertEqual(ctx.oks, ["The join DM carries the welcome picture once welcome pictures are on. Switch them on with `/greetings image on`."])
        await self.save(enabled=True)
        self.assertEqual((await self.run_command("image_dm", "on")).oks, ["The join DM now carries the welcome picture."])
        ctx = await self.run_command("image_dm", "off")
        self.assertFalse(self.settings("welcomecard")["dm_image"])
        self.assertEqual(ctx.oks, ["The join DM goes out without the welcome picture."])

    async def test_the_preview_goes_only_to_the_person_asking(self):
        self.admin.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{self.admin.id}/abc.png?size=1024")
        ctx = await self.run_command("image_preview")
        self.assertEqual((ctx.sent, self.channel.sent), ([], []), "nothing is posted where others can see")
        self.assertEqual(len(self.admin.dms), 1)
        sent = self.admin.dms[0]
        self.assertEqual(sent["file"].filename, "welcome.png")
        self.assertEqual(Image.open(sent["file"].fp).size, (1000, 300))
        self.assertIn("This is the welcome picture for you.", sent["content"])
        self.assertIn("Welcome pictures are off", sent["content"])
        self.assertFalse(sent["allowed_mentions"].users)
        self.assertEqual(ctx.oks, ["Sent the preview to your DMs."])
        self.assertEqual(self.outside.asked, [avatar_of(self.admin)])

    async def test_a_slash_preview_answers_where_only_the_asker_sees_it(self):
        ctx = self.context(author=self.admin)
        ctx.interaction = object()
        await self.run_command("image_preview", self.newcomer, ctx=ctx)
        self.assertEqual(len(ctx.sent), 1)
        sent = ctx.sent[0]
        self.assertIs(sent["ephemeral"], True)
        self.assertEqual(sent["file"].filename, "welcome.png")
        self.assertEqual(Image.open(sent["file"].fp).size, (1000, 300))
        self.assertIn(self.newcomer.mention, sent["content"])
        self.assertFalse(sent["allowed_mentions"].users, "naming the member pings nobody")
        self.assertEqual((self.admin.dms, self.newcomer.dms, self.channel.sent), ([], [], []), "no DM and no post in the channel")
        self.assertEqual((ctx.oks, ctx.fails), ([], []))

    async def test_a_slash_preview_goes_to_the_asker_past_a_redirect_the_servers_rules_set(self):
        # The bot's own context posts a command's answers where the server's rules redirect them. The preview, with the member's picture,
        # must not go there. FakeContext has no redirect to skip, so this uses the bot's context.
        ctx = BotContext.__new__(BotContext)
        ctx.author, ctx.guild, ctx.channel = self.admin, self.guild, self.channel
        ctx.interaction = types.SimpleNamespace(response=types.SimpleNamespace(defer=mock.AsyncMock(), is_done=lambda: True), followup=types.SimpleNamespace(send=mock.AsyncMock()))
        elsewhere = FakeChannel(self.guild, "bot-commands")
        ctx.decision = types.SimpleNamespace(muted=False, redirect=elsewhere.id, restricted_channel=None)
        answered = []

        async def to_the_asker(context, content=None, **kwargs):
            answered.append((content, kwargs))
        with mock.patch.object(commands.Context, "send", to_the_asker), mock.patch.object(context_module, "answer_place", lambda guild, target: elsewhere):
            await self.command("image_preview").callback(self.cards, ctx)
        self.assertEqual(elsewhere.sent, [], "nothing is posted in the channel the server's rules redirect to")
        self.assertEqual(len(answered), 1)
        self.assertIs(answered[0][1]["ephemeral"], True)
        self.assertEqual(answered[0][1]["file"].filename, "welcome.png")

    async def test_a_typed_preview_the_servers_rules_mute_draws_and_sends_nothing(self):
        ctx = self.context(author=self.admin)
        ctx.decision = type("Decision", (), {"muted": True})()
        with self.watch_draws() as drawn:
            await self.run_command("image_preview", ctx=ctx)
        self.assertEqual(drawn.call_count, 0)
        self.assertEqual((ctx.sent, ctx.oks, ctx.fails, self.admin.dms), ([], [], [], []))
        slash = self.context(author=self.admin)
        slash.decision = ctx.decision
        slash.interaction = object()
        await self.run_command("image_preview", ctx=slash)
        self.assertIs(slash.sent[0]["ephemeral"], True, "a slash command still answers the person asking")

    async def test_the_preview_can_be_drawn_for_another_member(self):
        await self.save(enabled=True, text="Hello {user}")
        with self.watch_draws() as drawn:
            await self.run_command("image_preview", self.newcomer)
        self.assertEqual(drawn.call_args.args[1].texts["text"].text, "Hello Sam")
        self.assertEqual(self.outside.asked, [avatar_of(self.newcomer)])
        self.assertEqual(len(self.admin.dms), 1)
        self.assertEqual(self.newcomer.dms, [], "the member being previewed hears nothing")
        self.assertIn(self.newcomer.mention, self.admin.dms[0]["content"])
        self.assertNotIn("Welcome pictures are off", self.admin.dms[0]["content"])

    async def test_a_preview_with_closed_dms_says_what_to_do(self):
        async def closed(*args, **kwargs):
            raise discord.Forbidden(type("Response", (), {"status": 403, "reason": "no"})(), "Cannot send messages to this user")
        self.admin.send = closed
        ctx = await self.run_command("image_preview")
        self.assertIn("Your DMs are closed", ctx.fails[0])

    async def test_a_preview_that_cant_be_drawn_says_why(self):
        with mock.patch.object(render, "render", side_effect=render.RenderError("The picture took too long to draw. Use fewer or smaller layers.")):
            ctx = await self.run_command("image_preview")
        self.assertEqual(ctx.fails, ["The picture wasn't drawn. The picture took too long to draw. Use fewer or smaller layers."])
        self.assertEqual(self.admin.dms, [])


class WithoutGreetingsTest(BotTest):
    modules = [WelcomeCard]

    async def test_it_loads_alone_and_adds_no_commands(self):
        self.assertIsNone(self.bot.get_command("greetings"))
        cards = self.cog(WelcomeCard)
        cards.outside = FakeOutside()
        await self.bot.settings.save(self.guild.id, "welcomecard", {"enabled": True})
        picture = await cards.picture_for(self.member, "welcome", Scope(guild=self.guild, member=self.member))
        self.assertEqual(picture["filename"], "welcome.png")


class RestoreTest(Case):
    async def test_a_restore_reloads_the_servers_pictures(self):
        key = (await self.cards.store_upload(self.guild.id, png(300, 100), "Kept"))["id"]
        await self.cards._upload(self.guild.id, key)
        await self.db.run("DELETE FROM welcome_images WHERE guild_id = ?", (self.guild.id,))
        await self.db.run("INSERT INTO welcome_images (guild_id, key, name, kind, width, height, frames, size, image, preview, created_at) VALUES (?, 'feedfacefeedface', 'Back', 'image/png', 64, 64, 1, 10, ?, '', 1)",
                          (self.guild.id, base64.b64encode(png(64, 64)).decode()))
        await self.cards.on_guild_restored(self.guild.id)
        self.assertEqual(self.cards._known[self.guild.id], {"feedfacefeedface"})
        self.assertIsNone(await self.cards._upload(self.guild.id, key), "the picture the backup didn't hold is gone from memory too")
        self.assertIsNotNone(await self.cards._upload(self.guild.id, "feedfacefeedface"))


class DemoTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.bot, app, self.closers = await build_demo(Path(self.folder.name))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.bot.timers.stop()
        for close in self.closers:
            await close()
        self.folder.cleanup()

    async def test_the_demo_opens_on_a_picture_that_is_set_up(self):
        settings = await (await self.client.get(f"/api/g/{GUILD}/settings/welcomecard")).json()
        self.assertTrue(settings["enabled"])
        listed = await (await self.client.get(f"/api/g/{GUILD}/welcomecard/uploads")).json()
        self.assertEqual([row["name"] for row in listed["uploads"]], ["Sample waves"])
        response = await self.client.post(f"/api/g/{GUILD}/welcomecard/preview", json={"draft": {}})
        self.assertEqual(response.status, 200, await response.text())
        answer = await response.json()
        self.assertEqual((answer["kind"], answer["width"], answer["height"]), ("png", 1000, 360))
        self.assertEqual((await self.client.get(listed["uploads"][0]["preview"])).status, 200)


SCRIPTS = Path(__file__).resolve().parent.parent / "captainguild" / "web" / "static" / "js"
PAGE_SCRIPTS = ("pages/welcomecard.js", "welcomecard/parts.js", "welcomecard/designer.js")
IMPORTED = re.compile(r"import \{([^}]*)\} from '([^']+)'")
EXPORTED = re.compile(r"^export (?:default )?(?:async )?(?:function|const|class|let) (\w+)", re.M)
EXPORTED_LIST = re.compile(r"^export \{([^}]*)\}", re.M)


def exported(path: Path) -> set[str]:
    """The names a script exports."""
    source = path.read_text(encoding="utf-8")
    names = set(EXPORTED.findall(source))
    for found in EXPORTED_LIST.findall(source):
        names.update(word.strip().split(" as ")[-1] for word in found.split(",") if word.strip())
    return names


class PageScriptsTest(unittest.TestCase):
    """No test runs the page's scripts, so what can be read from them is checked here. A name imported from a file that doesn't export it
    leaves the whole page blank."""

    def test_every_import_names_something_the_other_file_exports(self):
        wrong, seen = [], 0
        for name in PAGE_SCRIPTS:
            path = SCRIPTS / name
            for names, source in IMPORTED.findall(path.read_text(encoding="utf-8")):
                offered = exported((path.parent / source).resolve())
                for wanted in (word.strip().split(" as ")[0] for word in names.split(",") if word.strip()):
                    seen += 1
                    if wanted not in offered:
                        wrong.append(f"{name}: {wanted} from {source}")
        self.assertEqual(wrong, [])
        self.assertGreater(seen, 40, "the imports were found at all")

    def test_the_page_is_in_the_sidebar_under_greetings(self):
        nav = (SCRIPTS / "nav.js").read_text(encoding="utf-8")
        self.assertIn("{ key: 'welcomecard', name: 'Welcome image',", nav)
        self.assertLess(nav.index("key: 'greetings'"), nav.index("key: 'welcomecard'"))
        self.assertLess(nav.index("key: 'welcomecard'") - nav.index("key: 'greetings'"), 120, "right after the Greetings entry")
        self.assertRegex((SCRIPTS / "pages" / "welcomecard.js").read_text(encoding="utf-8"), r"export default function \w+\(")


if __name__ == "__main__":
    unittest.main()
