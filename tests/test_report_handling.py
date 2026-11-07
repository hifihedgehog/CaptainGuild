"""Who may handle a report, what a report keeps and shows, and the checklist's answers when something didn't work."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from typing import Any
from unittest import mock

import discord

from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.modules.reports import (
    ACTIONED, DEFAULT_LADDER, FALSE_POSITIVE, OPEN, REPORT_ACTIONED_ID, REPORT_FALSE_ID, REVIEW_DISMISS_ID, REVIEW_DONE_ID, REVIEW_START_ID, find_pack_role, ladder_command, ladder_label, parse_user_id, propose_action,
)
from captainguild.modules.moderation import Moderation
from captainguild.modules.reports import Reports
from tests.gateway import new_id
from tests.voice_report_harness import Harness
from tests.test_reports import DashboardTest, DeleteMessagesTest, GatherTest, ReportTest, ReviewTest, RoleToolsTest


def load_tests(loader: Any, tests: Any, pattern: Any) -> unittest.TestSuite:
    """Only this file's own tests. Its classes borrow their setup from classes in test_reports, whose tests run there."""
    def cases(suite: Any) -> Any:
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from cases(item)
            else:
                yield item
    return unittest.TestSuite(case for case in cases(tests) if type(case).__module__ == __name__ and case._testMethodName in type(case).__dict__)


class SubjectTest(ReviewTest):
    """A staffer reported by a member: the report and the review against them are for someone else to handle."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.accused = self.staff_id
        self.colleague = self.add_member("colleague", self.moderator_role["id"])

    async def report_the_staffer(self) -> tuple[dict, dict]:
        message = self.message_payload(user_id=self.accused, content="rude staffer")
        self.net.messages[int(message["id"])] = message
        await self.report_message(message, by=self.member_id)
        post = self.reports_posted()[-1]
        thread = self.thread_of(self.accused)
        self.state.parse_thread_create({**thread, "newly_created": True})
        await self.settle()
        return post, thread

    async def test_the_accused_cant_clear_the_report(self):
        post, _ = await self.report_the_staffer()
        for button in (REPORT_ACTIONED_ID, REPORT_FALSE_ID):
            await self.press(button, post, self.accused)
            self.assertIn("another moderator", self.last_answer())
        self.assertEqual((await self.rows())[0]["state"], OPEN)

    async def test_a_colleague_can(self):
        post, _ = await self.report_the_staffer()
        await self.press(REPORT_FALSE_ID, post, self.colleague)
        self.assertEqual((await self.rows())[0]["state"], FALSE_POSITIVE)

    async def test_an_administrator_can_handle_their_own(self):
        message = self.message_payload(user_id=self.admin_id, content="rude admin")
        self.net.messages[int(message["id"])] = message
        await self.report_message(message, by=self.member_id)
        await self.press(REPORT_FALSE_ID, self.reports_posted()[-1], self.admin_id)
        self.assertEqual((await self.rows())[0]["state"], FALSE_POSITIVE)

    async def test_the_accused_cant_touch_their_own_checklist(self):
        _, thread = await self.report_the_staffer()
        first = self.checklists(thread)[0]
        for button in (REVIEW_START_ID, REVIEW_DONE_ID, REVIEW_DISMISS_ID):
            await self.press(button, first, self.accused, channel=thread)
            self.assertIn("another moderator", self.last_answer())
        state = await self.state_of(thread)
        self.assertEqual((state["status"], state["current_step"]), ("active", 0))
        self.assertEqual(len(self.checklists(thread)), 1)

    async def test_nor_send_a_gather_form_left_open(self):
        _, thread = await self.report_the_staffer()
        first = self.checklists(thread)[0]
        await self.press(REVIEW_START_ID, first, self.colleague, channel=thread)
        await self.submit_form(self.accused, "1", "1", channel=thread)
        self.assertIn("another moderator", self.last_answer())
        self.assertEqual((await self.state_of(thread))["current_step"], 0)

    async def test_nor_restart_or_clear_it_some_other_way(self):
        post, thread = await self.report_the_staffer()
        await self.slash("review", self.accused, member=self.guild.get_member(self.accused))
        self.assertIn("another moderator", self.last_answer())
        self.assertIsNotNone((await self.state_of(thread))["report_id"])

    async def test_the_site_refuses_the_same_with_the_same_sentence(self):
        await self.report_the_staffer()
        row = (await self.rows())[0]
        client = await self.as_person(self.accused)
        for path, body in ((f"/reports/{row['id']}/state", {"state": "false_positive"}), (f"/reports/{row['id']}/case", {"case": None}), ("/reports/review", {"user_id": str(self.accused)})):
            response = await client.post(self.path(path), json=body)
            self.assertEqual(response.status, 403, path)
            self.assertIn("another moderator", (await response.json())["error"])
        self.assertEqual((await self.rows())[0]["state"], OPEN)
        client = await self.as_person(self.colleague)
        response = await client.post(self.path(f"/reports/{row['id']}/state"), json={"state": "actioned"})
        self.assertEqual(response.status, 200, await response.text())

    async def as_person(self, user_id: int, access: str = "staff") -> Any:
        from aiohttp.test_utils import TestClient, TestServer

        from captainguild.web.api import User
        from captainguild.web.app import build_app
        self.bot.ops.access_for = lambda guild_id, person: access if guild_id == self.guild_id else None
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(user_id, "someone", None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def test_reset_menu_refuses_the_accused_too(self):
        post, _ = await self.report_the_staffer()
        await self.press(REPORT_ACTIONED_ID, post, self.colleague)
        manager = self.add_role("Manager", 4, discord.Permissions(manage_guild=True).value)
        self.member_json[self.accused]["roles"].append(manager["id"])
        self.state.parse_guild_member_update({"guild_id": str(self.guild_id), **self.member_json[self.accused]})
        await self.message_menu("Reset Report", self.accused, self.net.messages[int(post["id"])])
        self.assertIn("another moderator", self.last_answer())
        self.assertEqual((await self.rows())[0]["state"], ACTIONED)


class ReopenTest(DashboardTest):
    async def test_staff_cant_reopen_on_the_site_as_they_cant_in_discord(self):
        rows = await self.seeded()
        await self.db.run("UPDATE reports SET state = 'actioned' WHERE id = ?", (rows[0]["id"],))
        client = await self.client(access="staff")
        response = await client.post(self.path(f"/reports/{rows[0]['id']}/state"), json={"state": "open"})
        self.assertEqual(response.status, 403)
        self.assertIn("Manage Server", (await response.json())["error"])
        client = await self.client(access="full")
        response = await client.post(self.path(f"/reports/{rows[0]['id']}/state"), json={"state": "open"})
        self.assertEqual(response.status, 200, await response.text())

    async def test_reopening_forgets_the_linked_case(self):
        rows = await self.seeded()
        await self.db.run("UPDATE reports SET state = 'actioned', case_number = 7 WHERE id = ?", (rows[0]["id"],))
        client = await self.client(access="full")
        response = await client.post(self.path(f"/reports/{rows[0]['id']}/state"), json={"state": "open"})
        self.assertIsNone((await response.json())["case_number"])

    async def test_bad_numbers_answer_400_not_a_stack_trace(self):
        await self.seeded()
        client = await self.client(access="staff")
        for tail in ("/reports?page=abc", "/reports?size=%C2%B2", "/reports/threads?page=%C2%B2"):
            self.assertEqual((await client.get(self.path(tail))).status, 400, tail)
        for tail in ("/reports?page=" + "9" * 30, "/reports?reporter=" + "9" * 30, "/reports?target=%C2%B2", "/reports/threads?member=" + "9" * 30):
            response = await client.get(self.path(tail))
            self.assertEqual(response.status, 200, tail)
        self.assert_no_errors()

    async def test_a_body_of_the_wrong_shape_is_refused(self):
        rows = await self.seeded()
        client = await self.client(access="staff")
        for path in (f"/reports/{rows[0]['id']}/state", f"/reports/{rows[0]['id']}/case", "/reports/review"):
            response = await client.post(self.path(path), data="[]", headers={"Content-Type": "application/json"})
            self.assertEqual(response.status, 400, path)
        self.assert_no_errors()

    def test_parse_user_id_takes_only_plain_numbers(self):
        self.assertIsNone(parse_user_id("\N{SUPERSCRIPT TWO}"))
        self.assertIsNone(parse_user_id("9" * 40))
        self.assertEqual(parse_user_id("<@!123>"), 123)


class DeleteReportTest(DashboardTest):
    async def test_full_access_deletes_the_report_and_its_post(self):
        rows = await self.seeded()
        row = rows[0]
        client = await self.client(access="full")
        response = await client.delete(self.path(f"/reports/{row['id']}"))
        self.assertEqual(response.status, 200, await response.text())
        self.assertIsNone(await self.reports.report_by_id(self.guild_id, row["id"]))
        self.assertTrue(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}"))
        activity = await self.db.all("SELECT action FROM activity WHERE guild_id = ?", (self.guild_id,))
        self.assertIn("report.delete", [a["action"] for a in activity])

    async def test_staff_cant(self):
        rows = await self.seeded()
        client = await self.client(access="staff")
        self.assertEqual((await client.delete(self.path(f"/reports/{rows[0]['id']}"))).status, 403)
        self.assertIsNotNone(await self.reports.report_by_id(self.guild_id, rows[0]["id"]))

    async def test_a_review_pointing_at_it_lets_go(self):
        rows = await self.seeded()
        row = rows[0]
        await self.reports._begin_review(555, self.guild_id, row["target_id"], row["id"])
        client = await self.client(access="full")
        await client.delete(self.path(f"/reports/{row['id']}"))
        self.assertIsNone((await self.reports._state(555))["report_id"])

    async def test_another_servers_report_is_out_of_reach(self):
        rows = await self.seeded()
        await self.db.run("UPDATE reports SET guild_id = ? WHERE id = ?", (self.guild_id + 1, rows[0]["id"]))
        client = await self.client(access="full")
        self.assertEqual((await client.delete(self.path(f"/reports/{rows[0]['id']}"))).status, 404)


class HiddenChannelSiteTest(DashboardTest):
    async def as_staffer(self) -> Any:
        from aiohttp.test_utils import TestClient, TestServer

        from captainguild.web.api import User
        from captainguild.web.app import build_app
        self.bot.ops.access_for = lambda guild_id, person: "staff" if guild_id == self.guild_id else None
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(self.staff_id, "staffer", None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def test_a_report_from_a_channel_they_cant_read_hides_the_message(self):
        rows = await self.seeded()
        row = rows[0]
        await self.db.run("UPDATE reports SET files = ? WHERE id = ?", (json.dumps([{"filename": "a.png", "name": "evidence-1.png", "size": 1, "content_type": "image/png", "copied": True}]), row["id"]))
        hidden = row["channel_id"]
        client = await self.as_staffer()

        def permissions_for(channel: Any, member: Any) -> discord.Permissions:
            return discord.Permissions.none() if channel.id == hidden else discord.Permissions.all()
        with mock.patch.object(discord.TextChannel, "permissions_for", permissions_for):
            body = await (await client.get(self.path(f"/reports/{row['id']}"))).json()
            listed = await (await client.get(self.path("/reports"))).json()
        self.assertTrue(body["content_hidden"])
        self.assertEqual((body["content"], body["files"]), ("", []))
        self.assertTrue(body["reason"], "The reason is what the reporter told staff")
        self.assertTrue(next(r for r in listed["rows"] if r["id"] == row["id"])["content_hidden"])

    async def test_a_search_doesnt_find_text_they_cant_read(self):
        rows = await self.seeded()
        row = rows[0]
        await self.db.run("UPDATE reports SET content = 'secretword' WHERE id = ?", (row["id"],))
        hidden = row["channel_id"]
        client = await self.as_staffer()

        def permissions_for(channel: Any, member: Any) -> discord.Permissions:
            return discord.Permissions.none() if channel.id == hidden else discord.Permissions.all()
        with mock.patch.object(discord.TextChannel, "permissions_for", permissions_for):
            found = await (await client.get(self.path("/reports?q=secretword"))).json()
        self.assertEqual(found["total"], 0)
        client = await self.client(access="staff")
        found = await (await client.get(self.path("/reports?q=secretword"))).json()
        self.assertEqual(found["total"], 1)


class PrivateThreadTest(ReportTest):
    async def test_the_history_button_reads_a_private_thread_as_discord_would(self):
        viewer = self.guild.get_member(self.staff_id)

        class Thread:
            id = 777
            type = discord.ChannelType.private_thread

            def __init__(self, allowed: discord.Permissions) -> None:
                self.allowed = allowed

            def permissions_for(self, member: Any) -> discord.Permissions:
                return self.allowed

        def lookup(thread: Any) -> Any:
            return lambda guild, channel_id: thread
        reading = discord.Permissions(view_channel=True, read_message_history=True)
        with mock.patch.object(discord.Guild, "get_channel_or_thread", lookup(Thread(reading))):
            self.assertFalse(self.reports._can_read(self.guild, viewer, 777))
        managing = discord.Permissions(view_channel=True, read_message_history=True, manage_threads=True)
        with mock.patch.object(discord.Guild, "get_channel_or_thread", lookup(Thread(managing))):
            self.assertTrue(self.reports._can_read(self.guild, viewer, 777))


class GatherScopeTest(GatherTest):
    def stored_in(self, text: str, channel_id: int) -> dict:
        return {**self.stored(text), "channel_id": channel_id}

    def hide(self, *channel_ids: int) -> Any:
        hidden = set(channel_ids)

        def permissions_for(channel: Any, member: Any) -> discord.Permissions:
            return discord.Permissions.none() if channel.id in hidden else discord.Permissions.all()
        return mock.patch.object(discord.TextChannel, "permissions_for", permissions_for)

    def embed_texts(self, thread: dict) -> list[str]:
        return [e.get("description") or "" for m in self.posts_in(thread["id"]) for e in m["embeds"]]

    async def test_messages_from_channels_the_presser_cant_read_stay_out_and_are_counted(self):
        thread, first = await self.reviewed()
        hidden = int(self.log_channel["id"])
        self.store.messages = [self.stored_in("open words", int(self.text["id"])), self.stored_in("hidden words", hidden)]
        with self.hide(hidden), mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "2", "1")
        texts = self.embed_texts(thread)
        self.assertIn("open words", texts)
        self.assertNotIn("hidden words", texts)
        self.assertTrue(any("Left out 1 item" in m["content"] for m in self.posts_in(thread["id"])))
        self.assert_no_errors()

    async def test_log_entries_from_those_channels_stay_out_too(self):
        thread, first = await self.reviewed()
        hidden = int(self.log_channel["id"])
        self.store.entries = [{"at": time.time(), "event": "delete", "channel_id": hidden, "user_id": self.member_id, "embed": {"title": "Message deleted", "description": "hidden log"}}]
        with self.hide(hidden), mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "1", "1")
        self.assertNotIn("hidden log", self.embed_texts(thread))

    async def test_a_scan_skips_channels_the_presser_cant_read(self):
        thread, first = await self.reviewed()
        hidden = int(self.log_channel["id"])
        said = self.net.message_json(hidden, {"content": "scanned hidden"})
        said["author"] = self.member_json[self.member_id]["user"]
        self.net.messages[int(said["id"])] = said
        with self.hide(hidden), mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "5", "1")
        self.assertNotIn("scanned hidden", self.embed_texts(thread))

    async def test_a_channel_the_moderator_role_cant_see_stays_out_for_an_administrator_too(self):
        thread, first = await self.reviewed()
        hidden = int(self.log_channel["id"])
        self.store.messages = [self.stored_in("admin only words", hidden)]
        moderator_role_id = int(self.moderator_role["id"])

        def permissions_for(channel: Any, who: Any) -> discord.Permissions:
            return discord.Permissions.none() if channel.id == hidden and getattr(who, "id", None) == moderator_role_id else discord.Permissions.all()
        modal_marks = len(self.net.modals)
        await self.press(REVIEW_START_ID, first, self.admin_id, channel=thread)
        self.assertEqual(len(self.net.modals), modal_marks + 1)
        with mock.patch.object(discord.TextChannel, "permissions_for", permissions_for), mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.submit_form(self.admin_id, "1", "1", channel=thread)
        self.assertNotIn("admin only words", self.embed_texts(thread))

    async def test_threads_they_cant_read_arent_named(self):
        thread, first = await self.reviewed()
        guild_thread = self.net.channels[int(thread["id"])]
        owned = {**guild_thread, "id": str(new_id()), "name": "secret plans", "owner_id": str(self.member_id), "type": 12, "parent_id": self.log_channel["id"]}
        self.net.channels[int(owned["id"])] = owned
        self.state.parse_thread_create({**owned, "newly_created": False})
        await self.settle()
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "1", "1")
        self.assertFalse(any("secret plans" in m["content"] for m in self.posts_in(thread["id"])))

    async def test_two_forms_at_once_gather_once(self):
        thread, first = await self.reviewed()
        self.store.messages = [self.stored("just once")]
        live = self.guild.get_thread(int(thread["id"]))
        turned_away: list[str] = []
        staff = self.guild.get_member(self.staff_id)

        class Response:
            async def defer(self) -> None:
                await asyncio.sleep(0.05)  # the round trip to Discord, which is where a second form can land

            async def send_message(self, text: str, **kwargs: Any) -> None:
                turned_away.append(text)

        class Followup:
            async def send(self, text: str, **kwargs: Any) -> None:
                pass

        class Answer:
            channel = live
            user = staff
            response = Response()
            followup = Followup()

        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await asyncio.gather(self.reports.submit_gather(Answer(), "1", "1"), self.reports.submit_gather(Answer(), "1", "1"))
            await self.settle()
        self.assertEqual(self.embed_texts(thread).count("just once"), 1)
        self.assertIn("already gathering", turned_away[0])

    async def test_it_tells_the_presser_what_it_posted(self):
        thread, first = await self.reviewed()
        self.store.messages = [self.stored("one")]
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "1", "1")
        self.assertIn("Posted 1 message", self.last_answer())
        self.assertTrue(self.is_private([a for a in self.answers() if "Posted" in (a.get("content") or "")][-1]))

    async def test_it_says_when_it_found_nothing(self):
        thread, first = await self.reviewed()
        for message_id, message in list(self.net.messages.items()):
            if message["author"]["id"] == str(self.member_id):
                del self.net.messages[message_id]
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "1", "1")
        self.assertIn("Found no messages", self.last_answer())
        self.assertEqual((await self.state_of(thread))["current_step"], 1)

    async def test_a_refused_batch_is_counted_in_the_thread(self):
        thread, first = await self.reviewed()
        self.store.messages = [self.stored(f"entry {n}") for n in range(12)]
        real = self.bot.http.request
        batches = 0

        async def first_batch_refused(route: Any, **kwargs: Any) -> Any:
            nonlocal batches
            if route.method == "POST" and route.path == "/channels/{channel_id}/messages" and len((kwargs.get("json") or {}).get("embeds") or []) == 10:
                batches += 1
                if batches == 1:
                    raise discord.HTTPException(type("R", (), {"status": 400, "reason": "no"})(), {"code": 0, "message": "no"})
            return await real(route, **kwargs)
        self.bot.http.request = first_batch_refused
        with mock.patch("captainguild.modules.reports.POST_PAUSE", 0), mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "12", "1")
        self.bot.http.request = real
        self.assertTrue(any("10 entries couldn't be posted" in m["content"] for m in self.posts_in(thread["id"])))
        self.errors.lines.clear()

    async def test_when_every_post_is_refused_it_stays_on_step_one(self):
        thread, first = await self.reviewed()
        self.store.messages = [self.stored("one")]
        await self.press(REVIEW_START_ID, first, self.staff_id, channel=thread)
        restore = self.failing("POST", "/channels/{channel_id}/messages", 403)
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.submit_form(self.staff_id, "1", "1", channel=thread)
        restore()
        self.assertEqual((await self.state_of(thread))["current_step"], 0)
        self.assertIn("step one", self.last_answer())
        self.errors.lines.clear()

    async def test_the_form_takes_a_thousand(self):
        thread, first = await self.reviewed()
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "1000", "1000")
        self.assertNotIn("between 1", self.last_answer())


class ChecklistAnswersTest(ReviewTest):
    async def test_a_step_that_cant_post_says_so(self):
        thread, first = await self.reviewed()
        restore = self.failing("POST", "/channels/{channel_id}/messages", 403)
        await self.click_done(thread)
        restore()
        self.assertIn("couldn't post", self.last_answer())
        self.assertEqual((await self.state_of(thread))["current_step"], 0)
        self.assert_no_errors()


class FilingAnswersTest(ReportTest):
    def snapshot_message(self, **snapshot: Any) -> dict:
        message = self.target_message("")
        message["message_reference"] = {"type": 1, "channel_id": self.text["id"], "message_id": str(new_id())}
        message["message_snapshots"] = [{"message": {"content": "", "embeds": [], "attachments": [], "type": 0, "timestamp": message["timestamp"], "edited_timestamp": None, "flags": 0, "mentions": [], "mention_roles": [], **snapshot}}]
        return message

    async def test_a_forwarded_message_keeps_its_words_and_files(self):
        message = self.snapshot_message(content="buy my thing", attachments=[self.attachment("scam.png")])
        await self.report_message(message)
        embed = self.reports_posted()[-1]["embeds"][0]
        self.assertIn("buy my thing", embed["description"])
        self.assertIn("Attachments: `1`", embed["description"])
        self.assertEqual(embed["image"]["url"], "attachment://evidence-1.png")
        self.assertEqual(self.net.uploads[-1][1], ["evidence-1.png"])
        self.assert_no_errors()

    async def test_slash_report_with_no_reason_opens_the_form(self):
        mark = len(self.net.modals)
        await self.slash("report", self.mod_id, member=self.guild.get_member(self.member_id))
        self.assertEqual(len(self.net.modals), mark + 1)
        await self.submit_form(self.mod_id, "line one\nline two")
        self.assertIn("line one", (await self.rows())[0]["reason"])

    async def test_a_typed_report_with_no_reason_comes_down_and_says_what_to_add(self):
        mark = len(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}"))
        self.dispatch_message(user_id=self.mod_id, content=f"!report <@{self.member_id}>")
        await self.settle()
        self.assertEqual(len(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}")), mark + 1)
        self.assertEqual(await self.rows(), [])
        self.assert_no_errors()

    async def test_a_typed_report_that_cant_run_comes_down_first(self):
        mark = len(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}"))
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"overwrites": {"report": {"enabled": False}}})
        self.dispatch_message(user_id=self.mod_id, content="!report nosuchperson he said a rude thing about me")
        await self.settle()
        self.assertEqual(len(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}")), mark + 1)
        bodies = [c[2] for c in self.calls_to("POST", "/channels/{channel_id}/messages")]
        self.assertEqual(len(bodies), 1, "One answer, from the report command and not the server-wide handler too")
        self.assertNotIn("rude thing", json.dumps(bodies))
        self.assert_no_errors()

    async def test_a_typed_report_the_bot_cant_remove_says_so(self):
        restore = self.failing("DELETE", "/channels/{channel_id}/messages/{message_id}", 403)
        self.dispatch_message(user_id=self.mod_id, content=f"!report <@{self.member_id}> spamming")
        await self.settle()
        restore()
        self.assertEqual(len(await self.rows()), 1)
        bodies = [c[2].get("content") or "" for c in self.calls_to("POST", "/channels/{channel_id}/messages")]
        self.assertTrue(any("can't remove your message" in body for body in bodies), bodies)

    async def test_the_confirmation_shows_the_image_reported(self):
        await self.report_message(self.target_message(attachments=[self.attachment("shot.png")]))
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertEqual(answer["embeds"][0]["image"]["url"], "https://cdn.example/shot.png")

    async def test_the_confirmation_never_shows_a_spoiler(self):
        await self.report_message(self.target_message(attachments=[self.attachment("SPOILER_shot.png")]))
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertNotIn("image", answer["embeds"][0])

    async def test_earlier_reports_show_on_the_post(self):
        first = self.add_member("witness")
        await self.report_message(self.target_message("first"), by=first)
        await self.press(REPORT_FALSE_ID, self.reports_posted()[-1], self.staff_id)
        await self.report_message(self.target_message("second"), by=self.add_member("witness2"))
        await self.report_message(self.target_message("third"), by=self.add_member("witness3"))
        self.assertIn("Earlier reports: `2` (`1` open)", self.reports_posted()[-1]["embeds"][0]["description"])
        self.assertNotIn("Earlier reports", self.reports_posted()[0]["embeds"][0]["description"])

    async def test_files_discord_refuses_are_named_as_not_copied(self):
        restore = self.failing("POST", "/channels/{channel_id}/messages", 413)
        original = self.bot.http.request

        async def only_with_files(route: Any, **kwargs: Any) -> Any:
            if kwargs.get("files"):
                return await original(route, **kwargs)
            restore()
            return await self.bot.http.request(route, **kwargs)
        self.bot.http.request = only_with_files
        await self.report_message(self.target_message(attachments=[self.attachment("big.png")]))
        restore()
        text = self.reports_posted()[-1]["embeds"][0]["description"]
        self.assertIn("Files not copied", text)
        self.assertNotIn("Files kept", text)
        row = (await self.rows())[0]
        self.assertIn("Files not copied", row["post_text"])
        kept = json.loads(row["files"])
        self.assertFalse(kept[0]["copied"])
        self.assertEqual(kept[0]["url"], "https://cdn.example/big.png")


class ReviewStatsTest(ReviewTest):
    async def test_the_stats_post_counts_reports(self):
        await self.report_message(self.target_message())
        stats = self.posts_in(self.thread_of(self.member_id)["id"])[0]["content"]
        self.assertIn("**Report Count:** 1 (1 open)", stats)


class GoneThingsTest(ReviewTest):
    async def test_a_deleted_thread_clears_the_link(self):
        thread, _ = await self.reviewed()
        self.state.parse_thread_delete({"id": thread["id"], "guild_id": str(self.guild_id), "parent_id": thread["parent_id"], "type": 11})
        await self.settle()
        self.assertIsNone((await self.rows())[0]["thread_id"])

    async def test_a_deleted_post_clears_the_link_and_a_purge_does_too(self):
        await self.report_message(self.target_message())
        await self.configure(forum_channel=None)
        post = self.reports_posted()[-1]
        self.state.parse_message_delete({"id": post["id"], "channel_id": post["channel_id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertIsNone((await self.rows())[0]["post_message_id"])
        self.forget_cooldowns()
        await self.report_message(self.target_message("two"))
        post = self.reports_posted()[-1]
        self.state.parse_message_delete_bulk({"ids": [post["id"]], "channel_id": post["channel_id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertIsNone((await self.rows())[1]["post_message_id"])


class LogCardTest(ReviewTest):
    async def test_a_message_edited_before_the_store_finds_its_card_in_the_old_log_channel(self):
        await self.configure(log_channel_fallback=int(self.log_channel["id"]))
        message = self.target_message("rude words")
        card = self.net.message_json(int(self.log_channel["id"]), {"embeds": [{"title": "Message deleted", "url": f"https://discord.com/channels/1/2/{message['id']}", "description": "old card"}]})
        self.net.messages[int(card["id"])] = card
        await self.report_message(message)
        thread = self.thread_of(self.member_id)
        self.assertTrue(any(e.get("description") == "old card" for m in self.posts_in(thread["id"]) for e in m["embeds"]))

    async def test_the_scan_stops_at_five_hundred(self):
        await self.configure(log_channel_fallback=int(self.log_channel["id"]))
        asked: list[int] = []
        real = discord.TextChannel.history

        def history(channel: Any, *args: Any, **kwargs: Any) -> Any:
            asked.append(kwargs.get("limit"))
            return real(channel, *args, **kwargs)
        with mock.patch.object(discord.TextChannel, "history", history):
            await self.reports._log_entry_for_message(self.guild, self.member_id, 12345)
        self.assertEqual(asked, [500])

    async def test_no_log_channel_means_no_scan(self):
        self.assertIsNone(await self.reports._log_entry_for_message(self.guild, self.member_id, 12345))


class LadderTextTest(ReportTest):
    def test_each_rung_carries_its_command(self):
        text = propose_action(DEFAULT_LADDER, {"warn": 0}, "<@1>")
        self.assertIn("/warn member:<@1>", text)
        self.assertIn("/mute member:<@1> duration:3d", text)
        self.assertIn("/tempban user:<@1> duration:7d", text)
        self.assertIn("/ban user:<@1>", text)

    def test_a_full_ladder_drops_the_syntax_rather_than_the_recommendation(self):
        ladder = [{"action": "tempban", "seconds": 86400 * (n + 2)} for n in range(12)]
        text = propose_action(ladder, {"warn": 0}, f"<@{'1' * 19}>")
        self.assertLessEqual(len(text), 1024)
        self.assertIn("Recommended Next Action", text)
        self.assertIn("**Command:**", text)
        self.assertNotIn("...", text)

    def test_counts_read_as_english(self):
        text = propose_action(DEFAULT_LADDER, {"warn": 1, "mute": 2, "tempban": 1}, "<@1>")
        self.assertIn("1 warn, 2 mutes, 1 temp ban", text)

    def test_a_rung_of_zero_names_what_the_command_really_does(self):
        rung = {"action": "mute", "seconds": 0}
        self.assertEqual(ladder_label(rung), "Indefinite mute")
        self.assertEqual(ladder_label(rung, 3600), "1-hour mute, this server's default")
        self.assertNotIn("duration", ladder_command(rung, "<@1>"))
        self.assertIn("1-hour mute, this server's default", propose_action([rung], {}, "<@1>", 3600))


class ServerDefaultTest(Harness):
    modules = [Moderation, Reports]

    async def test_step_two_reads_the_servers_mute_default(self):
        reports = self.bot.module("reports")
        await self.bot.settings.save(self.guild_id, "reports", {"ladder": [{"action": "mute", "seconds": 0}]})
        await self.bot.settings.save(self.guild_id, "moderation", {"mute": {**self.bot.module("moderation").cfg(self.guild_id)["mute"], "seconds": 3600}})
        embed = await reports._step_embed(self.guild, {"user_id": self.member_id, "report_id": None, "thread_id": 1}, 1)
        self.assertIn("1-hour mute, this server's default", embed.fields[0].value)


class DeleteMessagesEdgeTest(DeleteMessagesTest):
    async def test_a_typed_count_leaves_the_note_and_the_command_out(self):
        for index in range(5):
            said = self.message_payload(user_id=self.member_id, content=f"target {index}")
            self.net.messages[int(said["id"])] = said
        ids = {int(m["id"]) for m in self.posts_in(self.text["id"])}
        command = self.message_payload(user_id=self.admin_id, content="!delete_messages count: 2")
        self.net.messages[int(command["id"])] = command
        self.state.parse_message_create(command)
        await self.settle()
        left = {int(m["id"]) for m in self.posts_in(self.text["id"])} & ids
        self.assertEqual(len(ids) - len(left), 2)
        self.assertIn(int(command["id"]), self.net.messages, "The command itself was never one of the two")


class PackRoleTest(RoleToolsTest):
    def test_only_the_exact_name_matches(self):
        class Role:
            def __init__(self, name: str) -> None:
                self.name = name
        roles = [Role("Head Mod"), Role("Mod Squad")]
        self.assertIsNone(find_pack_role(roles, "Mod"))
        roles.append(Role("Mod"))
        self.assertEqual(find_pack_role(roles, "Mod").name, "Mod")
        apple = "\N{RED APPLE}"
        self.assertEqual(find_pack_role([Role(f"{apple} Mod")], "Mod", apple).name, f"{apple} Mod")
        self.assertIsNone(find_pack_role([Role(f"{apple} Mod")], "Mod"))

    async def test_delete_leaves_a_role_that_only_ends_the_same_way(self):
        head = self.add_role("Head Mod", 1)
        await self.configure(fun_roles=[{"name": "Mod", "emoji": ""}])
        await self.slash("delete_fun_roles", self.admin_id, dry_run=False)
        self.assertIn("No fun pack roles", self.last_answer())
        self.assertIn(int(head["id"]), self.net.roles)
        self.assertFalse(self.calls_to("DELETE", "/guilds/{guild_id}/roles/{role_id}"))

    async def test_the_group_holds_no_second_slash_spelling(self):
        group = next(c for c in self.bot.tree.get_commands() if c.name == "fun_roles")
        self.assertEqual([c.name for c in group.commands], ["list"])

    async def test_both_typed_spellings_work(self):
        await self.configure(fun_roles=[{"name": "Pack", "emoji": ""}])
        anchor = self.role_member["id"]
        mentions = {"mention_roles": [anchor]}
        for spelling in ("!deploy_fun_roles", "!fun_roles deploy"):
            self.dispatch_message(user_id=self.admin_id, content=f"{spelling} <@&{anchor}> prefix True", **mentions)
            await self.settle()
            self.assertIn("Dry run", self.calls_to("POST", "/channels/{channel_id}/messages")[-1][2].get("content") or "")
        self.assert_no_errors()
