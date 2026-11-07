"""The demo shows every page with something on it, and takes writes only from its own pages."""
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from captainguild.demo import GUILD, MAX_DEMO_UPLOAD, _local_only, build_demo


class DemoCase(unittest.IsolatedAsyncioTestCase):
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

    def path(self, tail):
        return f"/api/g/{GUILD}{tail}"

    async def get(self, tail):
        response = await self.client.get(self.path(tail))
        self.assertEqual(response.status, 200, tail)
        return await response.json()

    async def test_no_page_opens_empty(self):
        for tail, read in (
            ("/autopurge", lambda data: data), ("/embeds/messages", lambda data: data["rows"]), ("/embeds/scheduled", lambda data: data["rows"] if isinstance(data, dict) else data),
            ("/greetings/birthdays", lambda data: data), ("/reactionroles", lambda data: data["rr"]), ("/starboard/stats", lambda data: data["posts"]),
            ("/sticky", lambda data: data["stickies"]), ("/suggestions/list", lambda data: data["rows"]), ("/tags", lambda data: data["tags"]), ("/triggers", lambda data: data["triggers"]),
        ):
            self.assertTrue(read(await self.get(tail)), f"{tail} has no sample rows")

    async def test_the_pages_that_start_with_a_setup_card_open_on_their_forms(self):
        for module, field in (("suggestions", "channel_id"), ("starboard", "channel_id"), ("moderation", "modlog_channel"), ("moderation", "mute_role"), ("voicelink", "links"),
                              ("levels", "rewards"), ("proposals", "moderator_role")):
            self.assertTrue((await self.get(f"/settings/{module}"))[field], f"{module}.{field} is empty")

    async def test_every_change_in_the_activity_list_names_who_made_it(self):
        rows = await self.get("/activity")
        self.assertTrue(rows)
        self.assertTrue(all(row["user_name"] for row in rows), rows)

    async def test_a_mention_in_the_sample_log_names_someone_the_server_has(self):
        sample = await self.get("/members?ids=900000000000000301,900000000000000304")
        self.assertEqual(len(sample["members"]), 2)

    async def test_another_site_cant_write(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            response = await self.client.post(self.path("/autofeeds"), json={}, headers=headers)
            self.assertEqual(response.status, 403, headers)

    async def test_its_own_pages_can_write(self):
        host = f"http://{self.client.host}:{self.client.port}"
        for headers in ({"Origin": host}, {"Sec-Fetch-Site": "same-origin"}, {}):
            response = await self.client.post(self.path("/autofeeds"), json={}, headers=headers)
            self.assertNotEqual(response.status, 403, headers)

    async def test_a_large_upload_is_refused(self):
        request = make_mocked_request("POST", self.path("/backup"), headers={"Content-Length": str(MAX_DEMO_UPLOAD + 1)})
        answer = await _local_only(request, None)
        self.assertEqual(answer.status, 413)
