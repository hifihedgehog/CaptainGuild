import io
from unittest import mock

import aiohttp
from PIL import Image

from captainguild.core import images
from captainguild.core.ops import OpsError
from captainguild.core.validate import Invalid
from captainguild.modules.personalize import Personalize
from captainguild.web.app import MAX_REQUEST_BYTES
from tests.fakes import BotTest


def picture(width: int = 128, height: int = 128, kind: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(out, format=kind)
    return out.getvalue()


def form(**parts) -> aiohttp.FormData:
    body = aiohttp.FormData()
    for name, value in parts.items():
        if isinstance(value, bytes):
            body.add_field(name, value, filename=f"{name}.png", content_type="image/png")
        else:
            body.add_field(name, value, content_type="text/plain")  # a browser's FormData is always multipart, and this keeps it so
    return body


class PersonalizeTest(BotTest):
    modules = [Personalize]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.set_profile = mock.AsyncMock()
        self.bot.ops.set_bot_profile = self.set_profile

    async def test_starts_empty(self):
        client = await self.client()
        data = await (await client.get(self.path("/personalize"))).json()
        self.assertEqual(data, {"avatar": None, "banner": None, "bio": "", "custom": False})

    async def test_avatar_banner_and_bio_reach_discord_and_the_preview(self):
        client = await self.client()
        avatar, banner = picture(), picture(680, 240, "JPEG")
        response = await client.post(self.path("/personalize"), data=form(avatar=avatar, banner=banner, bio="  Here to help  "))
        self.assertEqual(response.status, 200)
        self.set_profile.assert_awaited_once_with(self.guild.id, avatar=avatar, banner=banner, bio="Here to help")
        data = await response.json()
        self.assertEqual(data["bio"], "Here to help")
        self.assertTrue(data["custom"])
        shown = await client.get(data["avatar"])
        self.assertEqual((shown.status, shown.content_type, await shown.read()), (200, "image/png", avatar))
        shown = await client.get(data["banner"])
        self.assertEqual((shown.status, shown.content_type, await shown.read()), (200, "image/jpeg", banner))
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "personalize.update")

    async def test_only_what_changed_goes_to_discord(self):
        client = await self.client()
        await client.post(self.path("/personalize"), data=form(avatar=picture(), bio="first"))
        self.set_profile.reset_mock()
        response = await client.post(self.path("/personalize"), data=form(bio="second"))
        self.set_profile.assert_awaited_once_with(self.guild.id, bio="second")
        data = await response.json()
        self.assertIsNotNone(data["avatar"], "changing the bio leaves the avatar alone")
        self.assertEqual(data["bio"], "second")
        await client.post(self.path("/personalize"), data=form(bio=""))
        self.assertEqual((await (await client.get(self.path("/personalize"))).json())["bio"], "")

    async def test_images_are_checked_by_their_bytes(self):
        client = await self.client()
        for name, data, phrase in (
            ("avatar", b"just text", "isn't an image"),
            ("avatar", picture(32, 32), "smallest"),
            ("banner", picture(5000, 64), "largest"),
            ("banner", picture(64, 5000), "largest"),
            ("avatar", picture(128, 128, "BMP"), "JPG, PNG or GIF"),
        ):
            response = await client.post(self.path("/personalize"), data=form(**{name: data}))
            self.assertEqual(response.status, 400, phrase)
            body = await response.json()
            self.assertIn(phrase, body["fields"][name])
        self.set_profile.assert_not_awaited()

    async def test_size_limit(self):
        client = await self.client()
        heavy = b"x" * (images.MAX_BYTES + 1)
        response = await client.post(self.path("/personalize"), data=form(avatar=heavy))
        self.assertEqual(response.status, 400)
        self.assertIn("8 MB", (await response.json())["error"])
        self.set_profile.assert_not_awaited()

    async def test_two_large_images_together_are_fine(self):
        # Each is under the limit, and together they pass the site's overall request size, which a part-by-part read doesn't count against.
        client = await self.client()
        out = io.BytesIO()
        Image.effect_noise((1700, 1700), 80).convert("RGB").save(out, format="PNG")
        large = out.getvalue()
        self.assertGreater(len(large) * 2, MAX_REQUEST_BYTES)
        self.assertLess(len(large), images.MAX_BYTES)
        response = await client.post(self.path("/personalize"), data=form(avatar=large, banner=large))
        self.assertEqual(response.status, 200)

    async def test_bio_limit_and_empty_form_and_wrong_body(self):
        client = await self.client()
        response = await client.post(self.path("/personalize"), data=form(bio="x" * 191))
        self.assertEqual(response.status, 400)
        self.assertIn("bio", (await response.json())["fields"])
        response = await client.post(self.path("/personalize"), data=form(other="x"))
        self.assertEqual(response.status, 400)
        response = await client.post(self.path("/personalize"), json={"bio": "x"})
        self.assertEqual(response.status, 400)

    async def test_a_refusal_from_discord_is_shown_and_nothing_is_stored(self):
        self.set_profile.side_effect = OpsError("Discord refused the profile change: too fast")
        client = await self.client()
        response = await client.post(self.path("/personalize"), data=form(avatar=picture()))
        self.assertEqual(response.status, 502)
        self.assertIn("too fast", (await response.json())["error"])
        self.assertFalse((await (await client.get(self.path("/personalize"))).json())["custom"])

    async def test_reset_clears_everything(self):
        client = await self.client()
        data = await (await client.post(self.path("/personalize"), data=form(avatar=picture(), bio="hi"))).json()
        response = await client.delete(self.path("/personalize"))
        self.assertEqual(response.status, 200)
        self.set_profile.assert_awaited_with(self.guild.id, avatar=None, banner=None, bio=None)
        self.assertEqual(await response.json(), {"avatar": None, "banner": None, "bio": "", "custom": False})
        self.assertEqual((await client.get(data["avatar"])).status, 404)

    async def test_only_full_access_can_touch_it(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/personalize"))).status, 403)
        self.assertEqual((await client.post(self.path("/personalize"), data=form(bio="x"))).status, 403)
        self.assertEqual((await client.delete(self.path("/personalize"))).status, 403)
        self.set_profile.assert_not_awaited()

    async def test_another_servers_images_are_out_of_reach(self):
        await self.db.run("INSERT INTO bot_profiles (guild_id, avatar, avatar_type, updated_at) VALUES (?, ?, ?, ?)", (self.guild.id + 1, "aGk=", "image/png", 1.0))
        client = await self.client()
        self.assertEqual((await client.get(self.path("/personalize/image/avatar"))).status, 404)
        self.assertEqual((await client.get(f"/api/g/{self.guild.id + 1}/personalize/image/avatar")).status, 403)

    async def test_the_image_check_itself(self):
        facts = images.check(picture(100, 200, "GIF"))
        self.assertEqual((facts.mime, facts.width, facts.height), ("image/gif", 100, 200))
        for bad in (b"", b"\x89PNG\r\n\x1a\n broken"):
            with self.assertRaises(Invalid):
                images.check(bad)
