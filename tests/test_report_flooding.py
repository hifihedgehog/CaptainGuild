"""Reports under pressure: one message reported by many, members reporting again and again, and files held in memory while posts go out."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from captainguild.modules import reports as reports_module
from captainguild.modules.reports import ACTIONED, ALSO_MAX, HELD_BYTES_MAX, OPEN, REPORT_ACTIONED_ID, REPORTS_PER_HOUR, REVIEW_START_ID, EvidenceHold, copy_attachments, full_report_text
from tests.gateway import new_id, user_json
from tests.test_reports import ReportTest, ReviewTest


class TwoReportersTest(ReportTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.others = [self.add_member(f"witness{n}") for n in range(3)]

    async def test_a_second_report_of_one_message_joins_the_first(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0], reason="first reason")
        pings = len([c for c in self.calls_to("POST", "/channels/{channel_id}/messages") if c[2].get("content") == f"<@&{self.alert['id']}>"])
        await self.report_message(message, by=self.others[1], reason="second reason")
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(len(self.reports_posted()), 1)
        after = len([c for c in self.calls_to("POST", "/channels/{channel_id}/messages") if c[2].get("content") == f"<@&{self.alert['id']}>"])
        self.assertEqual(after, pings, "No second ping")
        post = self.reports_posted()[-1]
        self.assertIn("Also reported by", post["embeds"][0]["description"])
        self.assertIn(f"<@{self.others[1]}>", post["embeds"][0]["description"])
        self.assertIn("second reason", post["embeds"][0]["description"])
        also = json.loads((await self.rows())[0]["also_reported"])
        self.assertEqual([a["id"] for a in also], [self.others[1]])
        self.assertEqual(also[0]["reason"], "second reason")
        self.assertIn("You reported", self.last_answer())
        self.assert_no_errors()

    async def test_handling_it_answers_both(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        await self.report_message(message, by=self.others[1])
        await self.press(REPORT_ACTIONED_ID, self.reports_posted()[-1], self.staff_id)
        self.assertEqual([r["state"] for r in await self.rows()], [ACTIONED])

    async def test_a_report_after_it_was_handled_is_a_new_report(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        await self.press(REPORT_ACTIONED_ID, self.reports_posted()[-1], self.staff_id)
        await self.report_message(message, by=self.others[1])
        self.assertEqual([r["state"] for r in await self.rows()], [ACTIONED, OPEN])

    async def test_the_same_person_again_adds_nothing(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        self.forget_cooldowns()
        await self.report_message(message, by=self.others[0])
        self.assertEqual(json.loads((await self.rows())[0]["also_reported"]), [])
        self.assertIn("You reported", self.last_answer())

    async def test_a_joined_report_reads_no_files(self):
        message = self.target_message(attachments=[self.attachment()])
        reads = 0
        real = self.net.cdn

        async def counted(url: str, **kwargs: Any) -> bytes:
            nonlocal reads
            reads += 1
            return await real(url)
        self.bot.http.get_from_cdn = counted
        await self.report_message(message, by=self.others[0])
        self.assertEqual(reads, 1)
        await self.report_message(message, by=self.others[1])
        self.assertEqual(reads, 1)

    async def test_two_reports_at_the_same_moment_make_one_post(self):
        message = self.target_message()
        answers = []
        for who in self.others[:2]:
            await self.message_menu("Report Message", who, message)
            modal = self.net.modals[-1]
            field = modal["components"][0].get("component") or modal["components"][0]["components"][0]
            rows = [{"type": 1, "components": [{"type": 4, "custom_id": field["custom_id"], "value": "at once"}]}]
            answers.append(self.interaction_payload(5, {"custom_id": modal["custom_id"], "components": rows, "resolved": {}}, user_id=who))
        for answer in answers:
            self.state.parse_interaction_create(answer)
        await self.settle()
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["also_count"], 1)

    async def test_a_report_on_a_user_joins_an_open_one_on_that_user(self):
        await self.user_menu("Report User", self.others[0], self.member_id)
        await self.submit_form(self.others[0], "bad name")
        await self.user_menu("Report User", self.others[1], self.member_id)
        await self.submit_form(self.others[1], "still a bad name")
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(json.loads((await self.rows())[0]["also_reported"])[0]["reason"], "still a bad name")

    async def test_a_report_with_an_image_stays_its_own(self):
        await self.user_menu("Report User", self.others[0], self.member_id)
        await self.submit_form(self.others[0], "bad name")
        await self.slash("report", self.others[1], member=self.guild.get_member(self.member_id), reason="here's proof", image={"filename": "proof.png", "content_type": "image/png"})
        self.assertEqual(len(await self.rows()), 2)

    async def test_a_message_report_doesnt_join_a_report_on_the_user(self):
        await self.user_menu("Report User", self.others[0], self.member_id)
        await self.submit_form(self.others[0], "bad name")
        await self.report_message(self.target_message(), by=self.others[1])
        self.assertEqual(len(await self.rows()), 2)

    async def test_a_report_whose_post_was_deleted_starts_again(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        post = self.reports_posted()[-1]
        self.net.messages.pop(int(post["id"]))
        self.state.parse_message_delete({"id": post["id"], "channel_id": post["channel_id"], "guild_id": str(self.guild_id)})
        await self.settle()
        await self.report_message(message, by=self.others[1])
        self.assertEqual(len(await self.rows()), 2)
        self.assertEqual(len(self.reports_posted()), 1)

    async def test_a_post_gone_without_an_event_starts_again(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        post = self.reports_posted()[-1]
        self.net.messages.pop(int(post["id"]))
        await self.report_message(message, by=self.others[1])
        self.assertEqual(len(await self.rows()), 2)

    async def test_the_list_of_names_stops_growing(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        row = (await self.rows())[0]
        crowd = json.dumps([{"id": 1000 + n, "name": f"n{n}", "reason": "x", "at": 1.0} for n in range(ALSO_MAX)])
        await self.db.run("UPDATE reports SET also_reported = ?, also_count = ? WHERE id = ?", (crowd, ALSO_MAX, row["id"]))
        edits = len(self.calls_to("PATCH", "/channels/{channel_id}/messages/{message_id}"))
        await self.report_message(message, by=self.others[1])
        fresh = (await self.rows())[0]
        self.assertEqual(fresh["also_count"], ALSO_MAX + 1)
        self.assertEqual(len(json.loads(fresh["also_reported"])), ALSO_MAX)
        self.assertEqual(len(self.calls_to("PATCH", "/channels/{channel_id}/messages/{message_id}")), edits, "Past the limit, the post stops being edited")

    async def test_many_names_never_push_out_who_handled_it(self):
        crowd = [{"id": 1000 + n, "name": "n" * 32, "reason": "r" * 120, "at": 1.0} for n in range(ALSO_MAX)]
        row = {"post_text": "p" * 3700, "state": ACTIONED, "handled_by": 5, "handled_name": "h", "reset_by": None, "also_reported": json.dumps(crowd), "also_count": 40, "case_number": 7}
        text = full_report_text(row)
        self.assertLessEqual(len(text), 4096)
        self.assertIn("Handled by", text)
        self.assertIn("Linked case: `7`", text)
        self.assertIn("more", text)

    async def test_the_site_shows_them_and_finds_them_by_id_or_name(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        await self.report_message(message, by=self.others[1], reason="me too")
        row = (await self.rows())[0]
        client = await self.client(access="staff")
        body = await (await client.get(self.path(f"/reports/{row['id']}"))).json()
        self.assertEqual(body["also_reported"][0]["reason"], "me too")
        found = await (await client.get(self.path(f"/reports?reporter={self.others[1]}"))).json()
        self.assertEqual(found["total"], 1)
        found = await (await client.get(self.path("/reports?reporter=witness1"))).json()
        self.assertEqual(found["total"], 1)
        found = await (await client.get(self.path("/reports?reporter=witness2"))).json()
        self.assertEqual(found["total"], 0)


class CapTest(ReportTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure(cooldown_seconds=0)
        self.reporter = self.add_member("reporter")

    async def test_a_member_can_file_so_many_reports_an_hour(self):
        for _ in range(REPORTS_PER_HOUR):
            await self.report_message(self.target_message(f"message {new_id()}"), by=self.reporter)
        self.assertEqual(len(await self.rows()), REPORTS_PER_HOUR)
        mark = len(self.net.modals)
        await self.message_menu("Report Message", self.reporter, self.target_message("one more"))
        self.assertIn("minutes", self.last_answer())
        self.assertEqual(len(self.net.modals), mark)

    async def test_manage_server_has_no_cap(self):
        member = self.guild.get_member(self.admin_id)
        for _ in range(REPORTS_PER_HOUR + 1):
            self.reports._start_cooldown(member)
        self.assertEqual(self.reports._cooldown_left(member), 0)

    async def test_the_hour_runs_out(self):
        member = self.guild.get_member(self.reporter)
        for _ in range(REPORTS_PER_HOUR):
            self.reports._start_cooldown(member)
        self.assertGreater(self.reports._cooldown_left(member), 3000)
        key = (self.guild_id, self.reporter)
        self.reports._filed[key] = [stamp - 3601 for stamp in self.reports._filed[key]]
        self.assertEqual(self.reports._cooldown_left(member), 0)

    async def test_a_burst_of_typed_reports_passes_no_cooldown(self):
        await self.configure(cooldown_seconds=3600)
        real = self.bot.http.request

        async def slow(route: Any, **kwargs: Any) -> Any:
            if route.method == "DELETE" and route.path == "/channels/{channel_id}/messages/{message_id}":
                await asyncio.sleep(0.05)
            return await real(route, **kwargs)
        self.bot.http.request = slow
        for index in range(4):
            target = self.add_member(f"target{index}")
            self.dispatch_message(user_id=self.reporter, content=f"!report <@{target}> number {index}", mentions=[self.member_json[target]["user"]])
        await self.settle(0.5)
        self.bot.http.request = real
        self.assertEqual(len(await self.rows()), 1)


class StrangerTest(ReportTest):
    def report_stranger(self, by: int) -> int:
        gone = new_id()
        data = {"id": str(new_id()), "name": "report", "type": 1, "options": [{"name": "member", "type": 6, "value": str(gone)}, {"name": "reason", "type": 3, "value": "Scam DMs"}], "resolved": {"users": {str(gone): user_json(gone, "stranger")}}}
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=by))
        return gone

    async def test_someone_never_here_gets_a_quiet_post_and_no_thread(self):
        await self.configure(forum_channel=int(self.forum["id"]))
        self.report_stranger(self.member_id)
        await self.settle()
        self.assertEqual(len(await self.rows()), 1)
        call = self.calls_to("POST", "/channels/{channel_id}/messages")[-1]
        self.assertFalse(call[2].get("content"))
        self.assertFalse(self.calls_to("POST", "/channels/{channel_id}/threads"))

    async def test_someone_with_a_thread_here_counts_as_known(self):
        await self.configure(forum_channel=int(self.forum["id"]))
        gone = new_id()
        await self.reports._index_thread(self.guild_id, gone, new_id(), "left")
        user = self.bot._connection.store_user(user_json(gone, "left"))
        self.assertTrue(await self.reports._known_here(self.guild, user))
        self.assertFalse(await self.reports._known_here(self.guild, self.bot._connection.store_user(user_json(new_id(), "never"))))


class PingGapTest(ReviewTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure(cooldown_seconds=0)
        self.others = [self.add_member(f"witness{n}") for n in range(3)]

    def pings(self, role: dict) -> int:
        return len([c for c in self.calls_to("POST", "/channels/{channel_id}/messages") if c[2].get("content") == f"<@&{role['id']}>"])

    async def test_a_flood_on_many_people_pings_once_a_minute(self):
        targets = [self.add_member(f"target{n}") for n in range(3)]
        for index, target in enumerate(targets):
            message = self.message_payload(user_id=target, content=f"rude {index}")
            self.net.messages[int(message["id"])] = message
            await self.report_message(message, by=self.others[index])
        self.assertEqual(len(self.reports_posted()), 3)
        self.assertEqual(self.pings(self.alert), 1)
        self.assertEqual(self.pings(self.moderator_role), 1)

    async def test_a_ping_comes_back_after_the_gap(self):
        await self.report_message(self.target_message(), by=self.others[0])
        for key in list(self.reports._pinged):
            self.reports._pinged[key] -= reports_module.PING_GAP + 1
        await self.report_message(self.target_message("another"), by=self.others[1])
        self.assertEqual(self.pings(self.alert), 2)

    async def test_a_new_report_keeps_the_step_of_a_running_review(self):
        thread, _ = await self.reviewed()
        await self.click_done(thread)
        await self.click_done(thread)
        await self.click_done(thread)
        self.assertEqual((await self.state_of(thread))["current_step"], 3)
        self.forget_cooldowns()
        for key in list(self.reports._pinged):
            self.reports._pinged[key] -= reports_module.PING_GAP + 1
        before = self.pings(self.moderator_role)
        await self.report_message(self.target_message("another one"), by=self.others[0])
        state = await self.state_of(thread)
        self.assertEqual((state["current_step"], state["status"]), (3, "active"))
        self.assertEqual(self.pings(self.moderator_role), before, "They're already on it")
        kept = self.checklists(thread)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["embeds"][0]["title"], "Step 4")
        posts = self.posts_in(thread["id"])
        self.assertTrue(any("another one" in m["content"] for m in posts))
        self.assertEqual(posts[-1]["id"], kept[0]["id"], "The checklist stays at the bottom")

    async def test_acknowledging_answers_every_open_report_in_the_review(self):
        thread, _ = await self.reviewed()
        for _ in range(3):
            await self.click_done(thread)
        self.forget_cooldowns()
        await self.report_message(self.target_message("another one"), by=self.others[0])
        rows = await self.rows()
        self.assertEqual([r["thread_id"] for r in rows], [int(thread["id"])] * 2)
        current = self.checklists(thread)[-1]
        self.assertFalse(self.buttons(current)["Acknowledge Report"]["disabled"])
        await self.press(REVIEW_START_ID, current, self.staff_id, channel=thread)
        self.assertEqual([r["state"] for r in await self.rows()], [ACTIONED, ACTIONED])

    async def test_staff_asking_for_a_review_still_restarts_it(self):
        thread, _ = await self.reviewed()
        await self.click_done(thread)
        await self.slash("review", self.staff_id, member=self.guild.get_member(self.member_id))
        self.assertEqual((await self.state_of(thread))["current_step"], 0)

    async def test_a_second_reporter_is_noted_in_the_thread_without_a_restart(self):
        message = self.target_message()
        await self.report_message(message, by=self.others[0])
        thread = self.thread_of(self.member_id)
        self.state.parse_thread_create({**thread, "newly_created": True})
        await self.settle()
        await self.click_done(thread)
        await self.report_message(message, by=self.others[1], reason="me as well")
        self.assertEqual((await self.state_of(thread))["current_step"], 1)
        self.assertTrue(any("me as well" in m["content"] for m in self.posts_in(thread["id"])))


class MemoryTest(ReportTest):
    def attachment_like(self, size: int) -> Any:
        class Attachment:
            filename = "big.png"
            content_type = "image/png"
            url = "https://cdn.example/big.png"

            def __init__(self, bytes_: int) -> None:
                self.size = bytes_
                self.read_count = 0

            def is_spoiler(self) -> bool:
                return False

            async def read(self) -> bytes:
                self.read_count += 1
                return b"x"

        return Attachment(size)

    async def test_files_held_have_a_limit_across_all_reports(self):
        hold = EvidenceHold(100)
        first = await copy_attachments([self.attachment_like(60)], 1000, hold)
        second = await copy_attachments([self.attachment_like(60)], 1000, hold)
        self.assertIsNotNone(first[0].data)
        self.assertIsNone(second[0].data, "Past the limit it's named as not copied")
        self.assertEqual(hold.held, 60)
        hold.release(first)
        self.assertEqual(hold.held, 0)
        self.assertIsNone(first[0].data)
        hold.release(first)
        self.assertEqual(hold.held, 0, "Letting go twice gives back once")

    async def test_a_file_that_fails_to_read_gives_its_room_back(self):
        hold = EvidenceHold(100)
        attachment = self.attachment_like(60)

        async def broken() -> bytes:
            raise OSError("no")
        attachment.read = broken
        await copy_attachments([attachment], 1000, hold)
        self.assertEqual(hold.held, 0)

    async def test_every_filing_lets_go_of_its_files(self):
        await self.report_message(self.target_message(attachments=[self.attachment()]))
        self.assertEqual(self.reports._held.held, 0)
        self.assertGreater(HELD_BYTES_MAX, reports_module.TOTAL_UPLOAD_BYTES)

    async def test_a_failed_filing_lets_go_too(self):
        message = self.target_message(attachments=[self.attachment()])
        await self.message_menu("Report Message", self.mod_id, message)
        restore = self.failing("POST", "/channels/{channel_id}/messages", 403)
        await self.submit_form(self.mod_id, "reason")
        restore()
        self.assertEqual(self.reports._held.held, 0)

    async def test_a_typed_report_lets_go_of_its_image(self):
        self.dispatch_message(user_id=self.mod_id, content=f"!report <@{self.member_id}> look at this", attachments=[self.attachment("proof.png")])
        await self.settle()
        self.assertEqual(self.net.uploads[-1][1][0], "evidence-1.png")
        self.assertEqual(self.reports._held.held, 0)
