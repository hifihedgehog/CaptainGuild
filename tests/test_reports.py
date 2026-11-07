"""Reports, review threads and the moved moderation tools, driven through the real library with the network faked."""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest import mock

import discord

from captainguild.modules.logs import EVENT_KINDS, Logs
from captainguild.modules.moderation import Moderation
from captainguild.modules.reports import (
    ACTIONED, DEFAULT_LADDER, DEFAULT_NOTICE, DISMISSED, DONE, FALSE_POSITIVE, OPEN, REPORT_ACTIONED_ID, REPORT_FALSE_ID, REPORT_HISTORY_ID, REVIEW_DISMISS_ID,
    REVIEW_DONE_ID, REVIEW_START_ID, Reports, build_notice, format_recap, format_rules_phrase, full_report_text, has_gif, in_pieces, ladder_label, parse_user_id, propose_action,
    recommended_step, uid_in_name,
)
from tests.fakes import FakeLogSink
from tests.gateway import iso, new_id, user_json
from tests.voice_report_harness import Harness

MEMBER_AT = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class StubLogging:
    """The two calls the review thread and the history button make of the logging module."""
    key = "logging"

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.entries: list[dict] = []
        self.asked: list[tuple] = []

    async def user_messages(self, guild_id: int, user_id: int, limit: int = 50) -> list[dict]:
        self.asked.append(("messages", user_id, limit))
        return [m for m in self.messages if m["author_id"] == user_id][:limit]

    async def user_log_entries(self, guild_id: int, user_id: int, limit: int = 50, kind: str | None = None) -> list[dict]:
        self.asked.append(("logs", user_id, limit))
        return [e for e in self.entries if e["user_id"] == user_id and (kind is None or EVENT_KINDS.get(e["event"]) == kind)][:limit]


class ReportTest(Harness):
    modules = [Reports]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.reports = self.bot.module("reports")
        self.reports_channel = self.log_channel
        self.alert = self.add_role("Alert", 1)
        self.moderator_role = self.add_role("Staff", 3, discord.Permissions(view_channel=True, read_message_history=True).value)
        self.staff_id = self.add_member("staffer", self.moderator_role["id"])
        await self.configure(report_channel=int(self.reports_channel["id"]), alert_role=int(self.alert["id"]), moderator_role=int(self.moderator_role["id"]))

    async def configure(self, **form: Any) -> None:
        await self.bot.settings.save(self.guild_id, "reports", form)

    def target_message(self, content: str = "something rude", **extra: Any) -> dict:
        message = self.message_payload(user_id=self.member_id, content=content, **extra)
        self.net.messages[int(message["id"])] = message
        return message

    def attachment(self, name: str = "shot one.png", content_type: str = "image/png") -> dict:
        return {"id": str(new_id()), "filename": name, "size": 10, "url": f"https://cdn.example/{name}", "proxy_url": f"https://cdn.example/{name}", "content_type": content_type}

    async def report_message(self, message: dict, *, by: int | None = None, reason: str = "It breaks the rules") -> None:
        by = by or self.mod_id
        await self.message_menu("Report Message", by, message)
        await self.submit_form(by, reason)

    def reports_posted(self) -> list[dict]:
        return [m for m in self.posts_in(self.reports_channel["id"]) if m["embeds"]]

    async def rows(self) -> list[dict]:
        return await self.db.all("SELECT * FROM reports ORDER BY id")

    def forget_cooldowns(self) -> None:
        self.reports._cooldowns.clear()


class FilingTest(ReportTest):
    async def test_message_report_carries_everything_the_post_should(self):
        message = self.target_message(reactions=[{"count": 2, "me": False, "emoji": {"id": None, "name": "x"}, "burst_colors": [], "count_details": {"burst": 0, "normal": 2}, "me_burst": False}])
        await self.report_message(message, reason="It breaks `the` rules")
        post = self.reports_posted()[-1]
        embed = post["embeds"][0]
        self.assertEqual(embed["author"]["name"], "Message Report")
        self.assertEqual(embed["color"], 0x2F3136)
        self.assertIn("thumbnail", embed)
        text = embed["description"]
        self.assertIn(f"<@{self.mod_id}>", text)
        self.assertIn(f"<@{self.member_id}>", text)
        self.assertIn(message["id"], text)
        self.assertIn(f"<#{self.text['id']}>", text)
        self.assertIn("Account created:", text)
        self.assertIn("Joined server:", text)
        self.assertIn("Attachments: `0`", text)
        self.assertIn("Reactions: `2`", text)
        self.assertIn("`something rude`", text)
        self.assertIn("It breaks the rules", text)  # backticks can't break out of the code span
        buttons = self.buttons(post)
        self.assertEqual(list(buttons), ["Jump to message", "Actioned", "False Positive", "View Message History"])
        self.assertTrue(buttons["Jump to message"]["url"].endswith(f"/{message['id']}"))
        self.assertFalse(buttons["Actioned"]["disabled"])
        self.assert_no_errors()

    async def test_only_the_alert_role_is_pinged(self):
        await self.report_message(self.target_message("@everyone look <@&123>"))
        post_call = self.calls_to("POST", "/channels/{channel_id}/messages")[-1]
        self.assertEqual(post_call[2]["content"], f"<@&{self.alert['id']}>")
        mentions = post_call[2]["allowed_mentions"]
        self.assertEqual(mentions.get("parse"), [])
        self.assertEqual(mentions.get("roles"), [self.alert["id"]] if isinstance(mentions["roles"][0], str) else [int(self.alert["id"])])

    async def test_no_alert_role_means_no_ping_at_all(self):
        await self.configure(alert_role=None)
        await self.report_message(self.target_message())
        post_call = self.calls_to("POST", "/channels/{channel_id}/messages")[-1]
        self.assertNotIn("content", {k: v for k, v in post_call[2].items() if v})
        self.assertEqual(post_call[2]["allowed_mentions"].get("parse"), [])

    async def test_the_reporter_gets_a_private_answer_with_a_jump_link(self):
        message = self.target_message()
        await self.report_message(message)
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertTrue(self.is_private(answer))
        self.assertIn("You reported", answer["embeds"][0]["description"])
        self.assertEqual(answer["components"][0]["components"][0]["label"], "Jump to message")

    async def test_attachments_are_copied_into_the_post_and_the_image_shows(self):
        message = self.target_message(attachments=[self.attachment("shot one.png"), self.attachment("notes.txt", "text/plain")])
        await self.report_message(message)
        self.assertEqual(self.net.uploads[-1], (int(self.reports_channel["id"]), ["evidence-1.png", "evidence-2.txt"]))
        embed = self.reports_posted()[-1]["embeds"][0]
        self.assertEqual(embed["image"]["url"], "attachment://evidence-1.png")
        self.assertIn("shot one.png", embed["description"])
        self.assertIn("Attachments: `2`", embed["description"])
        row = (await self.rows())[0]
        self.assertEqual([f["copied"] for f in json.loads(row["files"])], [True, True])

    async def test_an_attachment_that_cant_be_read_is_named_and_the_report_still_goes_out(self):
        async def broken(url: str, **kwargs: Any) -> bytes:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), {"code": 0, "message": "gone"})
        self.bot.http.get_from_cdn = broken
        await self.report_message(self.target_message(attachments=[self.attachment("gone.png")]))
        embed = self.reports_posted()[-1]["embeds"][0]
        self.assertIn("Files not copied", embed["description"])
        self.assertIn("gone.png", embed["description"])
        self.assertEqual(embed["image"]["url"], "https://cdn.example/gone.png")
        self.assert_no_errors()

    async def test_a_message_with_ten_files_keeps_all_ten_and_leaves_the_text_file_out(self):
        message = self.target_message("x" * 3000, attachments=[self.attachment(f"f{i}.png") for i in range(10)])
        await self.report_message(message)
        self.assertEqual(len(self.net.uploads[-1][1]), 10)
        self.assertNotIn("report.txt", self.net.uploads[-1][1])
        self.assertEqual(len((await self.rows())[0]["content"]), 3000)

    async def test_a_file_too_big_for_the_server_is_left_out(self):
        big = self.attachment("huge.png")
        big["size"] = 2 * 1024 * 1024 * 1024
        await self.report_message(self.target_message(attachments=[big]))
        self.assertEqual(self.net.uploads, [])
        self.assertIn("Files not copied", self.reports_posted()[-1]["embeds"][0]["description"])

    async def test_files_discord_refuses_as_too_large_dont_stop_the_report(self):
        restore = self.failing("POST", "/channels/{channel_id}/messages", 413)
        original = self.bot.http.request

        async def only_with_files(route: Any, **kwargs: Any) -> Any:
            if kwargs.get("files"):
                return await original(route, **kwargs)
            restore()
            return await self.bot.http.request(route, **kwargs)
        self.bot.http.request = only_with_files
        await self.report_message(self.target_message(attachments=[self.attachment()]))
        restore()
        post = self.reports_posted()[-1]
        self.assertEqual(post["embeds"][0]["image"]["url"], "https://cdn.example/shot one.png")

    async def test_long_content_is_cut_in_the_post_and_kept_whole_in_a_file(self):
        await self.report_message(self.target_message("x" * 3000))
        self.assertIn("report.txt", self.net.uploads[-1][1])
        embed = self.reports_posted()[-1]["embeds"][0]
        self.assertLessEqual(len(embed["description"]), 4096)
        row = (await self.rows())[0]
        self.assertEqual(len(row["content"]), 3000)

    async def test_a_bot_message_with_only_an_embed_still_shows_what_it_said(self):
        await self.report_message(self.target_message("", embeds=[{"title": "Win a prize", "description": "Click here", "type": "rich"}]))
        self.assertIn("Win a prize", self.reports_posted()[-1]["embeds"][0]["description"])

    async def test_cooldown_stops_a_second_report_but_not_manage_server(self):
        await self.report_message(self.target_message())
        await self.message_menu("Report Message", self.mod_id, self.target_message("again"))
        self.assertIn("Wait", self.last_answer())
        self.assertEqual(len(await self.rows()), 1)
        await self.report_message(self.target_message("from an admin"), by=self.admin_id)
        await self.report_message(self.target_message("from an admin again"), by=self.admin_id)
        self.assertEqual(len(await self.rows()), 3)

    async def test_cooldown_setting_of_zero_turns_it_off(self):
        await self.configure(cooldown_seconds=0)
        await self.report_message(self.target_message())
        await self.report_message(self.target_message("again"))
        self.assertEqual(len(await self.rows()), 2)

    async def test_the_limits_on_who_can_be_reported(self):
        await self.configure(report_self=False, report_bots=False, report_admins=False)
        own = self.message_payload(user_id=self.mod_id, content="mine")
        await self.message_menu("Report Message", self.mod_id, own)
        self.assertIn("yourself", self.last_answer())
        bot_id = self.add_member("otherbot", bot=True)
        await self.message_menu("Report Message", self.mod_id, self.message_payload(user_id=bot_id, content="beep"))
        self.assertIn("bot", self.last_answer())
        await self.message_menu("Report Message", self.mod_id, self.message_payload(user_id=self.admin_id, content="I run this"))
        self.assertIn("administrator", self.last_answer())
        await self.user_menu("Report User", self.mod_id, self.admin_id)
        self.assertIn("administrator", self.last_answer())
        self.assertEqual(self.net.modals, [])

    async def test_this_bots_own_message_cant_be_reported(self):
        await self.message_menu("Report Message", self.mod_id, self.message_payload(user_id=self.bot_id, content="hello"))
        self.assertIn("own", self.last_answer())

    async def test_someone_with_the_banned_role_cant_report(self):
        banned = self.add_role("No Reports", 1)
        blocked = self.add_member("blocked", banned["id"])
        await self.configure(banned_role=int(banned["id"]))
        await self.message_menu("Report Message", blocked, self.target_message())
        self.assertIn("role", self.last_answer())
        self.assertEqual(self.net.modals, [])

    async def test_no_reports_channel_says_so(self):
        await self.configure(report_channel=None)
        await self.user_menu("Report User", self.mod_id, self.member_id)
        self.assertIn("aren't set up", self.last_answer())
        self.assertEqual(self.net.modals, [])

    async def test_a_channel_that_was_deleted_says_so_and_leaves_no_row(self):
        await self.configure(report_channel=new_id())
        await self.user_menu("Report User", self.mod_id, self.member_id)
        self.assertIn("aren't set up", self.last_answer())
        self.assertEqual(await self.rows(), [])

    async def test_a_post_discord_refuses_leaves_no_row_and_tells_the_reporter(self):
        message = self.target_message()
        await self.message_menu("Report Message", self.mod_id, message)
        restore = self.failing("POST", "/channels/{channel_id}/messages", 403)
        await self.submit_form(self.mod_id, "reason")
        restore()
        self.assertEqual(await self.rows(), [])
        self.assertIn("couldn't reach", self.last_answer())
        self.assert_no_errors()

    async def test_report_user_menu_makes_a_member_report(self):
        await self.user_menu("Report User", self.mod_id, self.member_id)
        await self.submit_form(self.mod_id, "Bad profile picture")
        post = self.reports_posted()[-1]
        self.assertEqual(post["embeds"][0]["author"]["name"], "Member Report")
        self.assertNotIn("Reported message", post["embeds"][0]["description"])
        self.assertEqual(list(self.buttons(post)), ["Jump to where it was filed", "Actioned", "False Positive", "View Message History"])
        self.assertEqual((await self.rows())[0]["kind"], "user")

    async def test_slash_report_with_an_image(self):
        await self.slash("report", self.mod_id, member=self.guild.get_member(self.member_id), reason="ban evasion", image={"filename": "proof.png", "content_type": "image/png"})
        post = self.reports_posted()[-1]
        self.assertEqual(post["embeds"][0]["image"]["url"], "attachment://evidence-1.png")
        self.assertIn("ban evasion", post["embeds"][0]["description"])
        self.assertIn(f"<#{self.text['id']}>", post["embeds"][0]["description"])
        self.assertTrue(self.is_private([a for a in self.answers() if a.get("embeds")][-1]))
        self.assert_no_errors()

    async def test_text_report_comes_down_at_once_and_the_reporter_hears_privately(self):
        self.dispatch_message(user_id=self.mod_id, content=f"!report <@{self.member_id}> spamming links")
        await self.settle()
        self.assertTrue(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}"))
        self.assertIn("spamming links", self.reports_posted()[-1]["embeds"][0]["description"])
        self.assertTrue(self.calls_to("POST", "/users/@me/channels"))
        self.assert_no_errors()

    async def test_text_report_reads_its_image_before_taking_the_message_down(self):
        order: list[str] = []
        real_cdn, real_request = self.net.cdn, self.bot.http.request

        async def cdn(url: str, **kwargs: Any) -> bytes:
            order.append("read")
            return await real_cdn(url)

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == "DELETE" and route.path == "/channels/{channel_id}/messages/{message_id}":
                order.append("delete")
            return await real_request(route, **kwargs)
        self.bot.http.get_from_cdn = cdn
        self.bot.http.request = request
        self.dispatch_message(user_id=self.mod_id, content=f"!report <@{self.member_id}> look at this", attachments=[self.attachment("proof.png")])
        await self.settle()
        self.assertEqual(order[:2], ["read", "delete"])
        self.assertEqual(self.reports_posted()[-1]["embeds"][0]["image"]["url"], "attachment://evidence-1.png")

    async def test_a_text_report_keeps_an_image_up_to_discords_default_limit(self):
        """Discord raised the default upload limit to 20 MiB on September 3, 2026, and discord.py 2.7.1 still says 10 below boost level 2."""
        self.assertEqual(self.guild.filesize_limit, 10 * 1024 * 1024)
        image = dict(self.attachment("proof.png"), size=15 * 1024 * 1024)
        self.dispatch_message(user_id=self.mod_id, content=f"!report <@{self.member_id}> look at this", attachments=[image])
        await self.settle()
        self.assertEqual(self.reports_posted()[-1]["embeds"][0]["image"]["url"], "attachment://evidence-1.png")

    async def test_the_longest_possible_report_still_fits(self):
        big = [self.attachment("n" * 200 + f"{i}.png") for i in range(10)]
        await self.configure(forum_channel=int(self.forum["id"]))
        await self.report_message(self.target_message("c" * 4000, attachments=big), reason="r" * 2000)
        post = self.reports_posted()[-1]
        self.assertLessEqual(len(post["embeds"][0]["description"]), 4096 - 200, "room left for who handled it")
        await self.press(REPORT_ACTIONED_ID, post, self.staff_id)
        self.assertLessEqual(len([a for a in self.answers() if a.get("embeds")][-1]["embeds"][0]["description"]), 4096)
        self.assert_no_errors()

    async def test_a_webhook_post_can_be_reported_but_gets_no_review_thread(self):
        await self.configure(forum_channel=int(self.forum["id"]))
        webhook = self.message_payload(user_id=self.member_id, content="from a webhook", webhook_id=str(new_id()))
        webhook["author"] = user_json(new_id(), "Some Hook", bot=True)
        webhook.pop("member")
        await self.report_message(webhook)
        self.assertEqual(len(await self.rows()), 1)
        self.assertFalse(self.calls_to("POST", "/channels/{channel_id}/threads"))
        self.assert_no_errors()

    async def test_report_is_numbered_per_server_and_the_post_says_which(self):
        await self.report_message(self.target_message())
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        self.assertEqual([r["number"] for r in await self.rows()], [1, 2])
        self.assertIn("Report 2", self.reports_posted()[-1]["embeds"][0]["footer"]["text"])

    async def test_the_report_command_takes_someone_who_has_left(self):
        gone = new_id()
        data = {"id": str(new_id()), "name": "report", "type": 1, "options": [{"name": "member", "type": 6, "value": str(gone)}, {"name": "reason", "type": 3, "value": "Scam DMs, then left"}], "resolved": {"users": {str(gone): user_json(gone, "left_person")}}}
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=self.member_id))
        await self.settle()
        row = (await self.rows())[-1]
        self.assertEqual(row["target_id"], gone)
        self.assertIn("You reported", self.last_answer())
        self.assertIn("Joined server: `Unknown`", self.reports_posted()[-1]["embeds"][0]["description"])
        self.assert_no_errors()

    async def test_reportchannel_sets_the_channel(self):
        data = {"id": str(new_id()), "name": "reportchannel", "type": 1, "options": [{"name": "channel", "type": 7, "value": self.text["id"]}],
                "resolved": {"channels": {self.text["id"]: {"id": self.text["id"], "name": "general", "type": 0, "permissions": "0", "parent_id": self.text["parent_id"], "guild_id": str(self.guild_id), "nsfw": False, "position": 1, "rate_limit_per_user": 0, "topic": None, "flags": 0, "last_message_id": None}}}}
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=self.admin_id))
        await self.settle()
        self.assertEqual(self.reports.cfg(self.guild_id)["report_channel"], int(self.text["id"]))
        self.assert_no_errors()


class ButtonsTest(ReportTest):
    async def filed(self, message: dict | None = None) -> dict:
        await self.report_message(message or self.target_message())
        return self.reports_posted()[-1]

    async def test_actioned_recolors_names_the_handler_and_turns_buttons_off(self):
        post = await self.filed()
        await self.press(REPORT_ACTIONED_ID, post, self.staff_id)
        edit = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertEqual(edit["embeds"][0]["color"], 0x2ECC71)
        self.assertIn(f"Handled by <@{self.staff_id}>", edit["embeds"][0]["description"])
        parts = edit["components"][0]["components"]
        by_label = {p["label"]: p for p in parts}
        self.assertTrue(by_label["Actioned"]["disabled"])
        self.assertTrue(by_label["False Positive"]["disabled"])
        self.assertFalse(by_label["View Message History"].get("disabled", False))
        self.assertIn("url", by_label["Jump to message"])
        row = (await self.rows())[0]
        self.assertEqual((row["state"], row["handled_by"]), (ACTIONED, self.staff_id))

    async def test_false_positive(self):
        post = await self.filed()
        await self.press(REPORT_FALSE_ID, post, self.staff_id)
        edit = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertEqual(edit["embeds"][0]["color"], 0x992D22)
        self.assertIn("false report", edit["embeds"][0]["description"])
        self.assertEqual((await self.rows())[0]["state"], FALSE_POSITIVE)

    async def test_second_press_is_refused_and_changes_nothing(self):
        post = await self.filed()
        await self.press(REPORT_ACTIONED_ID, post, self.staff_id)
        await self.press(REPORT_FALSE_ID, post, self.admin_id)
        self.assertIn("already handled", self.last_answer())
        row = (await self.rows())[0]
        self.assertEqual((row["state"], row["handled_by"]), (ACTIONED, self.staff_id))

    async def test_two_presses_at_the_same_moment_have_one_winner(self):
        post = await self.filed()
        row = (await self.rows())[0]
        first, second = await asyncio.gather(
            self.reports.change_state(row, ACTIONED, self.guild.get_member(self.staff_id)),
            self.reports.change_state(row, FALSE_POSITIVE, self.guild.get_member(self.admin_id)),
        )
        self.assertEqual(sum(1 for r in (first, second) if r is not None), 1)

    async def test_a_member_who_can_see_the_channel_cant_press_them(self):
        post = await self.filed()
        await self.press(REPORT_ACTIONED_ID, post, self.member_id)
        self.assertIn("Only staff", self.last_answer())
        self.assertEqual((await self.rows())[0]["state"], OPEN)

    async def test_a_post_with_no_row_says_so(self):
        message = self.net.message_json(self.reports_channel["id"], {"embeds": [{"description": "old"}]})
        await self.press(REPORT_ACTIONED_ID, message, self.staff_id)
        self.assertIn("no longer tracked", self.last_answer())

    async def test_reset_report_needs_manage_server_and_reopens(self):
        post = await self.filed()
        await self.press(REPORT_ACTIONED_ID, post, self.staff_id)
        await self.message_menu("Reset Report", self.staff_id, post)
        self.assertIn("Manage Server", self.last_answer())
        await self.message_menu("Reset Report", self.admin_id, post)
        self.assertIn("has been reset", self.last_answer())
        edit = self.net.messages[int(post["id"])]
        self.assertEqual(edit["embeds"][0]["color"], 0x2F3136)
        self.assertIn("Reopened by", edit["embeds"][0]["description"])
        self.assertFalse(self.buttons(edit)["Actioned"]["disabled"])
        await self.press(REPORT_ACTIONED_ID, edit, self.staff_id)
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertNotIn("Reopened by", answer["embeds"][0]["description"])
        self.assertEqual((await self.rows())[0]["state"], ACTIONED)

    async def test_reset_says_so_on_a_report_not_yet_marked_and_on_other_messages(self):
        post = await self.filed()
        await self.message_menu("Reset Report", self.admin_id, post)
        self.assertIn("hasn't been marked", self.last_answer())
        await self.message_menu("Reset Report", self.admin_id, self.target_message())
        self.assertIn("only works on a report", self.last_answer())
        self.assert_no_errors()

    async def test_history_comes_from_the_store_and_hides_channels_the_viewer_cant_read(self):
        store = StubLogging()
        self.bot.modules.append(store)
        hidden = int(self.log_channel["id"])
        now = time.time()
        store.messages = [
            {"id": new_id(), "channel_id": int(self.text["id"]), "author_id": self.member_id, "content": "visible one", "attachments": [], "created_at": now, "deleted_at": now, "edited_at": None},
            {"id": new_id(), "channel_id": hidden, "author_id": self.member_id, "content": "hidden one", "attachments": [], "created_at": now, "deleted_at": None, "edited_at": None},
        ]
        post = await self.filed()
        calls_before = len(self.calls_to("GET", "/channels/{channel_id}/messages"))
        viewer = self.guild.get_member(self.staff_id)

        def permissions_for(self_channel: Any, member: Any) -> discord.Permissions:
            return discord.Permissions.none() if self_channel.id == hidden else discord.Permissions.all()
        with mock.patch.object(discord.TextChannel, "permissions_for", permissions_for):
            await self.press(REPORT_HISTORY_ID, post, viewer.id)
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertTrue(self.is_private(answer))
        self.assertIn("visible one", answer["embeds"][0]["description"])
        self.assertIn("deleted", answer["embeds"][0]["description"])
        self.assertNotIn("hidden one", answer["embeds"][0]["description"])
        self.assertEqual(len(self.calls_to("GET", "/channels/{channel_id}/messages")), calls_before)  # nothing was read from Discord

    async def test_history_reads_the_reported_channel_when_the_store_holds_nothing(self):
        self.bot.modules.append(StubLogging())
        other = self.add_member("otherbot", bot=True)
        said = self.message_payload(user_id=other, content="buy my thing")
        self.net.messages[int(said["id"])] = said
        post = await self.filed(said)
        await self.press(REPORT_HISTORY_ID, post, self.admin_id)
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertTrue(answer["embeds"][0]["description"].startswith("1 recent message from"))
        self.assertIn("buy my thing", answer["embeds"][0]["description"])
        self.assert_no_errors()

    async def test_that_channel_stays_shut_to_someone_who_cant_read_it(self):
        self.bot.modules.append(StubLogging())
        post = await self.filed()
        hidden = int(self.text["id"])

        def permissions_for(self_channel: Any, member: Any) -> discord.Permissions:
            return discord.Permissions.none() if self_channel.id == hidden else discord.Permissions.all()
        with mock.patch.object(discord.TextChannel, "permissions_for", permissions_for):
            await self.press(REPORT_HISTORY_ID, post, self.staff_id)
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertTrue(answer["embeds"][0]["description"].startswith("0 recent messages"))
        self.assertNotIn("something rude", answer["embeds"][0]["description"])

    async def test_history_too_long_for_an_embed_comes_with_a_file(self):
        store = StubLogging()
        self.bot.modules.append(store)
        store.messages = [{"id": new_id(), "channel_id": int(self.text["id"]), "author_id": self.member_id, "content": "y" * 280, "attachments": [], "created_at": time.time(), "deleted_at": None, "edited_at": None} for _ in range(40)]
        answer = await self.reports.history_answer(self.guild, self.guild.get_member(self.admin_id), self.member_id, "member")
        self.assertIn("file", answer)
        self.assertLessEqual(len(answer["embed"].description), 4096)

    async def test_history_without_the_logging_module_says_so(self):
        post = await self.filed()
        await self.press(REPORT_HISTORY_ID, post, self.staff_id)
        self.assertIn("isn't available", self.last_answer())
        self.assert_no_errors()

    async def test_buttons_answer_after_a_restart(self):
        # A restart leaves only what on_load registered, and every click must find its way by custom ID.
        post = await self.filed()
        persistent = self.bot.persistent_views
        ids = {child.custom_id for view in persistent for child in view.children}
        self.assertLessEqual({REPORT_ACTIONED_ID, REPORT_FALSE_ID, REPORT_HISTORY_ID, REVIEW_START_ID, REVIEW_DONE_ID, REVIEW_DISMISS_ID}, ids)
        self.assertNotIn(int(post["id"]), self.bot._connection._view_store._views)  # no per-message view shadows them


class ReviewTest(ReportTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure(forum_channel=int(self.forum["id"]))

    def threads(self) -> list[dict]:
        return [c for c in self.net.channels.values() if "thread_metadata" in c]

    def thread_of(self, user_id: int) -> dict:
        return next(t for t in self.threads() if t["name"].endswith(f"UID {user_id}"))

    def checklists(self, thread: dict) -> list[dict]:
        return [m for m in self.posts_in(thread["id"]) if m["embeds"] and m["embeds"][0].get("title", "").startswith("Step ")]

    async def reviewed(self) -> tuple[dict, dict]:
        """File a report, so the member has a thread with a checklist at step one."""
        await self.report_message(self.target_message())
        thread = self.thread_of(self.member_id)
        self.state.parse_thread_create({**thread, "newly_created": True})
        await self.settle()
        return thread, self.checklists(thread)[0]

    async def state_of(self, thread: dict) -> dict:
        return await self.reports._state(int(thread["id"]))

    def live_thread(self, thread: dict) -> Any:
        return self.guild.get_thread(int(thread["id"]))

    async def click_done(self, thread: dict, *, by: int | None = None) -> None:
        current = self.checklists(thread)[-1]
        await self.press(REVIEW_DONE_ID, current, by or self.staff_id, channel=thread)


class ThreadTest(ReviewTest):
    async def test_report_opens_a_named_thread_with_the_stats_ping_report_message_and_checklist(self):
        message = self.target_message("the rude words")
        await self.report_message(message)
        thread = self.thread_of(self.member_id)
        self.assertEqual(thread["name"], f"member UID {self.member_id}")
        posts = self.posts_in(thread["id"])
        self.assertIn(f"**User:** <@{self.member_id}>", posts[0]["content"])
        self.assertIn("**Warn Count:** 0", posts[0]["content"])
        self.assertEqual(posts[1]["content"], f"<@&{self.moderator_role['id']}>")
        self.assertEqual(posts[2]["embeds"][0]["author"]["name"], "Message Report")
        self.assertIn("the rude words", posts[3]["content"])
        self.assertIn("/" + message["id"], posts[3]["content"])
        self.assertEqual(posts[4]["embeds"][0]["title"], "Step 1")
        self.assertEqual(list(self.buttons(posts[4])), ["Gather Recent Messages & Logs", "Mark Step Completed", "Dismiss"])
        self.assert_no_errors()

    async def test_the_ping_reaches_only_the_moderator_role_and_nothing_else_pings(self):
        await self.report_message(self.target_message("<@1> @everyone"))
        thread = self.thread_of(self.member_id)
        bodies = [c[2] for c in self.calls_to("POST", "/channels/{channel_id}/messages")]
        ping = next(b for b in bodies if b.get("content") == f"<@&{self.moderator_role['id']}>")
        self.assertEqual(ping["allowed_mentions"].get("parse"), [])
        copy = next(b for b in bodies if "Reported message from" in (b.get("content") or ""))
        self.assertEqual(copy["allowed_mentions"]["parse"], [])

    async def test_attachments_go_into_the_thread_too(self):
        await self.report_message(self.target_message(attachments=[self.attachment()]))
        thread = self.thread_of(self.member_id)
        self.assertIn((int(thread["id"]), ["evidence-1.png"]), self.net.uploads)

    async def test_a_second_report_reuses_the_thread_and_keeps_the_step_with_one_checklist(self):
        thread, first = await self.reviewed()
        await self.click_done(thread)
        self.assertEqual((await self.state_of(thread))["current_step"], 1)
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        self.assertEqual(len(self.threads()), 1)
        kept = self.checklists(thread)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["embeds"][0]["title"], "Step 2")
        self.assertEqual((await self.state_of(thread))["current_step"], 1)

    async def test_a_new_report_reopens_a_finished_or_dismissed_review(self):
        thread, first = await self.reviewed()
        await self.press(REVIEW_DISMISS_ID, first, self.staff_id, channel=thread)
        self.assertEqual((await self.state_of(thread))["status"], DISMISSED)
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        state = await self.state_of(thread)
        self.assertEqual((state["status"], state["current_step"]), ("active", 0))
        self.assertEqual(len(self.checklists(thread)), 1)

    async def test_two_reports_at_once_make_one_thread(self):
        member = self.guild.get_member(self.member_id)
        await asyncio.gather(self.reports.open_review(self.guild, member), self.reports.open_review(self.guild, member))
        self.assertEqual(len(self.threads()), 1)
        self.assertEqual(len(self.checklists(self.threads()[0])), 1)

    async def test_an_archived_thread_comes_back_for_a_new_report(self):
        thread, _ = await self.reviewed()
        thread["thread_metadata"]["archived"] = True
        self.state.parse_thread_update({**thread})
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        patches = [c for c in self.calls_to("PATCH", "/channels/{channel_id}") if c[2].get("archived") is False]
        self.assertTrue(patches)
        self.assertEqual(len(self.threads()), 1)

    async def test_failed_lookup_of_a_known_thread_never_makes_a_second_thread(self):
        thread, _ = await self.reviewed()
        self.state.parse_thread_delete({"id": thread["id"], "guild_id": str(self.guild_id), "parent_id": thread["parent_id"], "type": 11})
        await asyncio.sleep(0)
        await self.reports._index_thread(self.guild_id, self.member_id, int(thread["id"]), "member")
        restore = self.failing("GET", "/channels/{channel_id}", 500)
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        restore()
        self.assertEqual(len(self.threads()), 1)
        self.assertEqual(len(await self.rows()), 2)  # the report itself still went through
        self.assert_no_errors()

    async def test_a_thread_someone_deleted_gets_replaced(self):
        thread, _ = await self.reviewed()
        self.net.channels.pop(int(thread["id"]))
        self.state.parse_thread_delete({"id": thread["id"], "guild_id": str(self.guild_id), "parent_id": thread["parent_id"], "type": 11})
        await self.settle()
        self.assertIsNone(await self.state_of(thread))
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        self.assertEqual(len(self.threads()), 1)
        self.assertNotEqual(self.threads()[0]["id"], thread["id"])

    async def test_existing_threads_are_found_at_startup_and_reused(self):
        active = self.net_thread(f"member UID {self.member_id}", self.mod_id)
        self.state.parse_thread_create({**active, "newly_created": False})
        old_archived = self.net_thread(f"member-old UID {self.member_id}", self.mod_id, archived=True)
        other = self.net_thread(f"someone UID {self.admin_id}", self.mod_id, archived=True)
        ignored = self.net_thread("no id in this one", self.mod_id, archived=True)
        self.net.archived[int(self.forum["id"])] = [old_archived, other, ignored]
        found = await self.reports.load_thread_index(self.guild)
        self.assertEqual(found, 2)
        rows = await self.db.all("SELECT user_id, thread_id FROM review_threads ORDER BY user_id")
        self.assertEqual({r["user_id"]: r["thread_id"] for r in rows}, {self.member_id: int(active["id"]), self.admin_id: int(other["id"])})
        await self.report_message(self.target_message())
        self.assertFalse(self.calls_to("POST", "/channels/{channel_id}/threads"))
        self.assertEqual(self.checklists(active)[0]["embeds"][0]["title"], "Step 1")

    def net_thread(self, name: str, owner: int, *, archived: bool = False) -> dict:
        thread = {"id": str(new_id()), "guild_id": str(self.guild_id), "parent_id": self.forum["id"], "owner_id": str(owner), "name": name, "type": 11, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": archived, "auto_archive_duration": 10080, "archive_timestamp": iso(), "locked": False}, "flags": 0}
        self.net.channels[int(thread["id"])] = thread
        return thread

    async def test_a_new_thread_created_by_hand_in_the_forum_is_noticed(self):
        thread = self.net_thread(f"member UID {self.member_id}", self.mod_id)
        self.state.parse_thread_create({**thread, "newly_created": True})
        await self.settle()
        await self.report_message(self.target_message())
        self.assertEqual(self.threads()[0]["id"], thread["id"])
        self.assertFalse(self.calls_to("POST", "/channels/{channel_id}/threads"))

    async def test_a_thread_from_an_earlier_bot_loses_its_dead_checklists_and_gets_our_stats(self):
        thread = self.net_thread(f"member UID {self.member_id}", self.mod_id)
        self.state.parse_thread_create({**thread, "newly_created": False})
        await self.reports.load_thread_index(self.guild)
        earlier = user_json(new_id(), "EarlierBot", bot=True)
        stale = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 2", "description": "Review previous mutes or warns and propose an action."}]})
        stale["author"] = earlier
        keep = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 9", "description": "Not one of ours"}]})
        keep["author"] = earlier
        self.net.messages[int(stale["id"])] = stale
        self.net.messages[int(keep["id"])] = keep
        await self.report_message(self.target_message())
        self.assertNotIn(int(stale["id"]), self.net.messages)
        self.assertIn(int(keep["id"]), self.net.messages)
        self.assertTrue(any("**User:**" in m["content"] for m in self.posts_in(thread["id"])))

    async def test_a_second_review_edits_the_stats_post_instead_of_adding_one(self):
        thread, _ = await self.reviewed()
        stats_count = sum(1 for m in self.posts_in(thread["id"]) if "**User:**" in m["content"])
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        self.assertEqual(sum(1 for m in self.posts_in(thread["id"]) if "**User:**" in m["content"]), stats_count)
        self.assertTrue(self.calls_to("PATCH", "/channels/{channel_id}/messages/{message_id}"))

    async def test_counts_come_from_the_moderation_history(self):
        record = {"cases": [{"number": 7, "action": "mute", "seconds": 90000, "created_at": 1700000000.0, "reason": "x"}, {"number": 6, "action": "hardmute", "seconds": 0, "created_at": 1699000000.0, "reason": "y"}],
                  "notes": [], "counts": {"warn": 3, "mute": 2, "tempban": 1, "ban": 0, "kick": 4, "timeout": 5}}

        class Stub:
            key = "moderation"

            async def history(self, guild_id: int, user_id: int) -> dict:
                return record
        self.bot.modules.append(Stub())
        await self.report_message(self.target_message())
        stats = self.posts_in(self.thread_of(self.member_id)["id"])[0]["content"]
        for line in ("**Warn Count:** 3", "**Mute Count:** 2", "**Tempban Count:** 1", "**Timeout Count:** 5", "**Kick Count:** 4"):
            self.assertIn(line, stats)
        self.assertIn("Duration: 2 days", stats)
        self.assertIn("Duration: indefinite", stats)

    async def test_a_lock_stays_on_when_a_new_report_comes_in(self):
        thread, _ = await self.reviewed()
        thread["thread_metadata"]["locked"] = True
        self.state.parse_thread_update({**thread})
        self.forget_cooldowns()
        await self.report_message(self.target_message("again"))
        self.assertEqual([c for c in self.calls_to("PATCH", "/channels/{channel_id}") if c[1] == "/channels/{channel_id}"], [], "a thread that's only locked is left as it is")
        thread["thread_metadata"]["archived"] = True
        self.state.parse_thread_update({**thread})
        self.forget_cooldowns()
        await self.report_message(self.target_message("and again"))
        self.assertEqual([c[2] for c in self.calls_to("PATCH", "/channels/{channel_id}") if c[1] == "/channels/{channel_id}"][-1], {"archived": False})

    async def test_moving_the_forum_starts_a_new_thread_there_and_keeps_using_it(self):
        first, _ = await self.reviewed()
        moved = {**self.forum, "id": str(new_id()), "name": "reviews-2"}
        self.net.channels[int(moved["id"])] = moved
        self.state.parse_channel_create(moved)
        await self.configure(forum_channel=int(moved["id"]))
        await self.settle()
        self.forget_cooldowns()
        await self.report_message(self.target_message("after the move"))
        theirs = [t for t in self.threads() if t["name"].endswith(f"UID {self.member_id}")]
        self.assertEqual(len(theirs), 2)
        newest = next(t for t in theirs if t["parent_id"] == moved["id"])
        self.assertTrue(any("after the move" in m["content"] for m in self.posts_in(newest["id"])))
        self.assertFalse(any("after the move" in m["content"] for m in self.posts_in(first["id"])))
        self.state.parse_thread_create({**newest, "newly_created": True})
        await self.settle()
        self.forget_cooldowns()
        await self.report_message(self.target_message("a third"))
        self.assertEqual(len([t for t in self.threads() if t["name"].endswith(f"UID {self.member_id}")]), 2)
        self.assertTrue(any("a third" in m["content"] for m in self.posts_in(newest["id"])))
        self.assert_no_errors()

    async def test_a_reason_too_long_for_the_post_goes_into_the_thread_whole(self):
        reason = "start " + "r" * 1500 + " end"
        await self.report_message(self.target_message(), reason=reason)
        posts = self.posts_in(self.thread_of(self.member_id)["id"])
        self.assertTrue(any(reason in m["content"] for m in posts))
        self.assertTrue(any("Full reason" in m["content"] for m in posts))
        self.assert_no_errors()

    async def test_a_reason_that_fits_isnt_repeated(self):
        await self.report_message(self.target_message(), reason="Short and clear")
        self.assertFalse(any("Full reason" in m["content"] for m in self.posts_in(self.thread_of(self.member_id)["id"])))

    async def test_a_review_staff_open_doesnt_carry_an_old_report(self):
        thread, first = await self.reviewed()
        self.assertIsNotNone((await self.state_of(thread))["report_id"])
        await self.press(REVIEW_DISMISS_ID, first, self.staff_id, channel=thread)
        await self.slash("review", self.staff_id, member=self.guild.get_member(self.member_id))
        self.assertIsNone((await self.state_of(thread))["report_id"])
        for _ in range(3):
            await self.click_done(thread)
        step = self.checklists(thread)[-1]
        self.assertEqual(step["embeds"][0]["title"], "Step 4")
        self.assertTrue(self.buttons(step)["Acknowledge Report"]["disabled"])
        self.assertNotIn("Jump to report", step["embeds"][0]["fields"][0]["value"])
        self.assert_no_errors()

    async def test_a_forum_that_requires_a_tag_gets_one(self):
        tag = {"id": str(new_id()), "name": "Review", "moderated": False, "emoji_id": None, "emoji_name": None}
        self.state.parse_channel_update({**self.forum, "flags": 16, "available_tags": [tag]})
        await self.report_message(self.target_message())
        body = self.calls_to("POST", "/channels/{channel_id}/threads")[-1][2]
        self.assertEqual([str(t) for t in body["applied_tags"]], [tag["id"]])
        self.assert_no_errors()

    async def test_a_forum_that_refuses_a_thread_doesnt_stop_the_report(self):
        restore = self.failing("POST", "/channels/{channel_id}/threads", 403)
        await self.report_message(self.target_message())
        restore()
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(self.threads(), [])
        self.assert_no_errors()
        restore = self.failing("POST", "/channels/{channel_id}/threads", 403)
        await self.slash("review", self.staff_id, member=self.guild.get_member(self.member_id))
        restore()
        self.assertIn("couldn't be opened", self.last_answer())
        self.assert_no_errors()

    async def test_no_forum_means_the_report_still_goes_out(self):
        await self.configure(forum_channel=None)
        await self.report_message(self.target_message())
        self.assertEqual(self.threads(), [])
        self.assertEqual(len(await self.rows()), 1)

    async def test_a_forum_setting_pointing_at_a_text_channel_is_ignored_cleanly(self):
        await self.configure(forum_channel=int(self.text["id"]))
        await self.report_message(self.target_message())
        self.assertEqual(self.threads(), [])
        self.assert_no_errors()

    async def test_auto_review_off_opens_no_thread_but_staff_can_ask(self):
        await self.configure(auto_review=False)
        await self.report_message(self.target_message())
        self.assertEqual(self.threads(), [])
        await self.slash("review", self.staff_id, member=self.guild.get_member(self.member_id))
        self.assertEqual(self.checklists(self.thread_of(self.member_id))[0]["embeds"][0]["title"], "Step 1")
        self.assertIn("review is open", self.last_answer())
        self.assert_no_errors()

    async def test_a_review_can_be_asked_for_as_text(self):
        self.dispatch_message(user_id=self.staff_id, content=f"!review <@{self.member_id}>", mentions=[self.member_json[self.member_id]["user"]])
        await self.settle()
        self.assertEqual(self.checklists(self.thread_of(self.member_id))[0]["embeds"][0]["title"], "Step 1")
        self.assert_no_errors()

    async def test_only_staff_can_ask_for_a_review(self):
        await self.slash("review", self.member_id, member=self.guild.get_member(self.admin_id))
        self.assertIn("Only staff", self.last_answer())
        self.assertEqual(self.threads(), [])

    async def test_review_of_someone_who_left_uses_their_user(self):
        gone = new_id()
        user = self.bot._connection.store_user(user_json(gone, "gone_person"))
        thread = await self.reports.open_review(self.guild, user)
        self.assertEqual(thread.name, f"gone_person UID {gone}")
        posts = self.posts_in(thread.id)
        self.assertIn("Not in the server now", posts[0]["content"])
        await self.click_done(self.net.channels[thread.id])
        self.assertEqual((await self.reports._state(thread.id))["current_step"], 1)
        self.assert_no_errors()


class ChecklistTest(ReviewTest):
    async def test_steps_walk_through_and_finishing_deletes_the_checklist_and_says_nothing(self):
        thread, _ = await self.reviewed()
        titles = []
        for _ in range(4):
            await self.click_done(thread)
            current = self.checklists(thread)
            self.assertEqual(len(current), 1, "one tracked checklist per thread")
            titles.append(current[0]["embeds"][0]["title"])
        self.assertEqual(titles, ["Step 2", "Step 3", "Step 4", "Step 5"])
        before = len(self.posts_in(thread["id"]))
        await self.click_done(thread)
        after = self.posts_in(thread["id"])
        self.assertEqual(self.checklists(thread), [])
        self.assertEqual(len(after), before - 1, "finishing posts nothing")
        state = await self.state_of(thread)
        self.assertEqual((state["status"], state["modal_message_id"]), (DONE, None))
        answers = [a for a in self.answers() if a.get("content") and "complete" in a["content"].lower()]
        self.assertEqual(answers, [])

    async def test_the_replacement_is_posted_before_the_old_one_is_deleted(self):
        thread, first = await self.reviewed()
        mark = len(self.net.calls)
        await self.click_done(thread)
        kinds = [(c[0], c[1]) for c in self.net.calls[mark:] if "messages" in c[1] and "webhooks" not in c[1] and "interactions" not in c[1]]
        self.assertEqual(kinds, [("POST", "/channels/{channel_id}/messages"), ("DELETE", "/channels/{channel_id}/messages/{message_id}")])

    async def test_a_failed_post_keeps_the_old_checklist_and_the_step(self):
        thread, first = await self.reviewed()
        restore = self.failing("POST", "/channels/{channel_id}/messages", 500)
        await self.click_done(thread)
        restore()
        self.assertEqual([m["id"] for m in self.checklists(thread)], [first["id"]])
        state = await self.state_of(thread)
        self.assertEqual((state["current_step"], state["modal_message_id"]), (0, int(first["id"])))
        self.assert_no_errors()

    async def test_two_moderators_clicking_at_once_get_one_checklist_and_one_step(self):
        thread, first = await self.reviewed()
        live = self.live_thread(thread)
        await asyncio.gather(self.reports.show_step(live, 1, expected=0), self.reports.show_step(live, 1, expected=0))
        self.assertEqual(len(self.checklists(thread)), 1)
        self.assertEqual((await self.state_of(thread))["current_step"], 1)

    async def test_two_clicks_that_both_read_the_same_step_dont_skip_one(self):
        thread, first = await self.reviewed()
        payload = self.interaction_payload(3, {"custom_id": REVIEW_DONE_ID, "component_type": 2}, user_id=self.staff_id, channel=thread, message=first)
        other = self.interaction_payload(3, {"custom_id": REVIEW_DONE_ID, "component_type": 2}, user_id=self.admin_id, channel=thread, message=first)
        self.state.parse_interaction_create(payload)
        self.state.parse_interaction_create(other)
        await self.settle()
        self.assertEqual((await self.state_of(thread))["current_step"], 1)
        self.assertEqual(len(self.checklists(thread)), 1)

    async def test_reconcile_posts_nothing_while_the_checklist_exists_whoever_spoke_last(self):
        thread, first = await self.reviewed()
        chat = self.message_payload(user_id=self.staff_id, content="I'll take this one", channel=thread)
        self.net.messages[int(chat["id"])] = chat
        self.state.parse_message_create(chat)
        await self.settle()
        mark = len(self.calls_to("POST", "/channels/{channel_id}/messages"))
        for _ in range(3):
            await self.reports.reconcile()
        self.assertEqual(len(self.calls_to("POST", "/channels/{channel_id}/messages")), mark)
        self.assertEqual([m["id"] for m in self.checklists(thread)], [first["id"]])

    async def test_a_deleted_checklist_comes_back_once_at_the_same_step(self):
        thread, first = await self.reviewed()
        await self.click_done(thread)
        current = self.checklists(thread)[0]
        self.net.messages.pop(int(current["id"]))
        self.state.parse_message_delete({"id": current["id"], "channel_id": thread["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertEqual(self.reports._flagged, {int(thread["id"])})
        await self.reports._reconcile_loop.coro(self.reports)
        back = self.checklists(thread)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["embeds"][0]["title"], "Step 2")
        await self.reports.reconcile()
        self.assertEqual(len(self.checklists(thread)), 1)

    async def test_a_bulk_delete_that_took_the_checklist_is_noticed(self):
        thread, first = await self.reviewed()
        self.net.messages.pop(int(first["id"]))
        self.state.parse_message_delete_bulk({"ids": [first["id"], str(new_id())], "channel_id": thread["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertEqual(self.reports._flagged, {int(thread["id"])})
        await self.reports._reconcile_loop.coro(self.reports)
        self.assertEqual(len(self.checklists(thread)), 1)

    async def test_our_own_replacement_deleting_the_old_message_flags_nothing(self):
        thread, first = await self.reviewed()
        await self.click_done(thread)
        self.state.parse_message_delete({"id": first["id"], "channel_id": thread["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        self.assertEqual(self.reports._flagged, set())

    async def test_a_failed_lookup_isnt_a_missing_message(self):
        thread, first = await self.reviewed()
        restore = self.failing("GET", "/channels/{channel_id}/messages/{message_id}", 503)
        await self.reports.reconcile()
        restore()
        self.assertEqual([m["id"] for m in self.checklists(thread)], [first["id"]])
        self.assertEqual((await self.state_of(thread))["modal_message_id"], int(first["id"]))
        self.assert_no_errors()

    async def test_archived_threads_are_never_written_to_by_background_work(self):
        thread, first = await self.reviewed()
        self.net.messages.pop(int(first["id"]))
        thread["thread_metadata"]["archived"] = True
        self.state.parse_thread_update({**thread})
        mark = len(self.calls_to("POST", "/channels/{channel_id}/messages"))
        await self.reports.reconcile()
        await self.reports.sweep_checklists()
        self.assertEqual(len(self.calls_to("POST", "/channels/{channel_id}/messages")), mark)
        self.assertEqual(self.calls_to("PATCH", "/channels/{channel_id}"), [])

    async def test_an_uncached_archived_thread_is_found_and_left_alone(self):
        thread, first = await self.reviewed()
        self.net.messages.pop(int(first["id"]))
        thread["thread_metadata"]["archived"] = True
        self.guild._threads.pop(int(thread["id"]), None)
        mark = len(self.calls_to("POST", "/channels/{channel_id}/messages"))
        await self.reports.reconcile([int(thread["id"])])
        self.assertEqual(len(self.calls_to("POST", "/channels/{channel_id}/messages")), mark)

    async def test_a_pass_over_every_review_makes_no_lookups_for_threads_it_doesnt_hold(self):
        thread, first = await self.reviewed()
        self.guild._threads.pop(int(thread["id"]), None)
        mark = len(self.calls_to("GET", "/channels/{channel_id}"))
        await self.reports.reconcile()
        self.assertEqual(len(self.calls_to("GET", "/channels/{channel_id}")), mark)

    async def test_a_thread_that_no_longer_exists_clears_its_state(self):
        thread, first = await self.reviewed()
        self.guild._threads.pop(int(thread["id"]), None)
        real = self.bot.http.request

        async def gone(route: Any, **kwargs: Any) -> Any:
            if route.method == "GET" and route.path == "/channels/{channel_id}":
                raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), {"code": 10003, "message": "Unknown Channel"})
            return await real(route, **kwargs)
        self.bot.http.request = gone
        await self.reports.reconcile([int(thread["id"])])
        self.bot.http.request = real
        self.assertIsNone(await self.state_of(thread))

    async def test_dismiss_is_permanent(self):
        thread, first = await self.reviewed()
        await self.press(REVIEW_DISMISS_ID, first, self.staff_id, channel=thread)
        self.assertIn("will not post again", self.last_answer())
        self.assertEqual(self.checklists(thread), [])
        state = await self.state_of(thread)
        self.assertEqual((state["status"], state["modal_message_id"]), (DISMISSED, None))
        mark = len(self.posts_in(thread["id"]))
        await self.reports.reconcile()
        await self.reports.sweep_checklists()
        self.assertEqual(len(self.posts_in(thread["id"])), mark)

    async def test_dismiss_from_an_old_copy_removes_the_current_one_too(self):
        thread, first = await self.reviewed()
        await self.click_done(thread)
        old_copy = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 1", "description": "x"}]})
        self.net.messages[int(old_copy["id"])] = old_copy
        await self.press(REVIEW_DISMISS_ID, old_copy, self.staff_id, channel=thread)
        self.assertEqual(self.checklists(thread), [])

    async def test_a_stale_copy_deletes_itself_when_clicked_and_changes_nothing(self):
        thread, first = await self.reviewed()
        await self.click_done(thread)
        stale = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 1", "description": "x"}]})
        self.net.messages[int(stale["id"])] = stale
        await self.press(REVIEW_DONE_ID, stale, self.staff_id, channel=thread)
        self.assertIn("older copy", self.last_answer())
        self.assertNotIn(int(stale["id"]), self.net.messages)
        self.assertEqual((await self.state_of(thread))["current_step"], 1)

    async def test_a_leftover_in_a_closed_or_unknown_review_deletes_itself(self):
        thread, first = await self.reviewed()
        await self.press(REVIEW_DISMISS_ID, first, self.staff_id, channel=thread)
        leftover = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 1", "description": "x"}]})
        self.net.messages[int(leftover["id"])] = leftover
        await self.press(REVIEW_DONE_ID, leftover, self.staff_id, channel=thread)
        self.assertIn("already marked dismissed", self.last_answer())
        self.assertNotIn(int(leftover["id"]), self.net.messages)
        await self.db.run("DELETE FROM review_state WHERE thread_id = ?", (int(thread["id"]),))
        leftover = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 1", "description": "x"}]})
        self.net.messages[int(leftover["id"])] = leftover
        await self.press(REVIEW_START_ID, leftover, self.staff_id, channel=thread)
        self.assertIn("already closed", self.last_answer())
        self.assertNotIn(int(leftover["id"]), self.net.messages)
        self.assert_no_errors()

    async def test_only_staff_can_use_the_checklist(self):
        thread, first = await self.reviewed()
        for button in (REVIEW_START_ID, REVIEW_DONE_ID, REVIEW_DISMISS_ID):
            await self.press(button, first, self.member_id, channel=thread)
            self.assertIn("Only staff", self.last_answer())
        self.assertEqual((await self.state_of(thread))["status"], "active")
        self.assertIn(int(first["id"]), self.net.messages)

    async def test_buttons_stay_off_that_only_name_a_step(self):
        thread, _ = await self.reviewed()
        expected = {"Step 2": ("Propose Action", True), "Step 3": ("Create Ticket", True), "Step 4": ("Acknowledge Report", False), "Step 5": ("Process Completed", True)}
        for _ in range(4):
            await self.click_done(thread)
            current = self.checklists(thread)[0]
            label, off = expected[current["embeds"][0]["title"]]
            self.assertEqual(self.buttons(current)[label]["disabled"], off)

    async def test_the_ladder_recommends_on_step_two(self):
        thread, _ = await self.reviewed()
        await self.click_done(thread)
        field = self.checklists(thread)[0]["embeds"][0]["fields"][0]
        self.assertEqual(field["name"], "Moderation Recommendation")
        self.assertIn("Warning", field["value"])
        self.assertIn(f"/warn member:<@{self.member_id}>", field["value"])

    async def test_ticket_step_uses_the_servers_command(self):
        await self.configure(ticket_command="/ticket open {user}")
        thread, _ = await self.reviewed()
        await self.click_done(thread)
        await self.click_done(thread)
        self.assertEqual(self.checklists(thread)[0]["embeds"][0]["fields"][0]["value"], f"/ticket open <@{self.member_id}>")

    async def test_acknowledge_marks_the_report_handled_and_moves_on(self):
        thread, _ = await self.reviewed()
        for _ in range(3):
            await self.click_done(thread)
        current = self.checklists(thread)[0]
        self.assertIn("still open", current["embeds"][0]["fields"][0]["value"])
        self.assertIn("Jump to report 1", current["embeds"][0]["fields"][0]["value"])
        await self.press(REVIEW_START_ID, current, self.staff_id, channel=thread)
        row = (await self.rows())[0]
        self.assertEqual((row["state"], row["handled_by"]), (ACTIONED, self.staff_id))
        post = self.net.messages[row["post_message_id"]]
        self.assertEqual(post["embeds"][0]["color"], 0x2ECC71)
        self.assertTrue(self.buttons(post)["Actioned"]["disabled"])
        self.assertEqual(self.checklists(thread)[0]["embeds"][0]["title"], "Step 5")

    async def test_step_four_says_when_the_report_is_already_handled(self):
        thread, _ = await self.reviewed()
        await self.press(REPORT_ACTIONED_ID, self.reports_posted()[-1], self.admin_id)
        for _ in range(3):
            await self.click_done(thread)
        current = self.checklists(thread)[0]
        self.assertIn("already been marked by admin", current["embeds"][0]["fields"][0]["value"])
        self.assertTrue(self.buttons(current)["Acknowledge Report"]["disabled"])

    async def test_sweep_keeps_the_newest_copy_and_corrects_its_step_in_place(self):
        thread, first = await self.reviewed()
        await self.click_done(thread)
        current = self.checklists(thread)[0]
        newest = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 1", "description": "x"}]})
        self.net.messages[int(newest["id"])] = newest
        mark = len(self.calls_to("POST", "/channels/{channel_id}/messages"))
        await self.reports.sweep_checklists()
        kept = self.checklists(thread)
        self.assertEqual([m["id"] for m in kept], [newest["id"]])
        self.assertEqual(kept[0]["embeds"][0]["title"], "Step 2")
        self.assertEqual(len(self.calls_to("POST", "/channels/{channel_id}/messages")), mark, "corrected by editing, with no new post")
        self.assertEqual((await self.state_of(thread))["modal_message_id"], int(newest["id"]))

    async def test_sweep_clears_every_copy_from_a_closed_review(self):
        thread, first = await self.reviewed()
        await self.press(REVIEW_DISMISS_ID, first, self.staff_id, channel=thread)
        copy = self.net.message_json(int(thread["id"]), {"embeds": [{"title": "Step 3", "description": "x"}]})
        self.net.messages[int(copy["id"])] = copy
        await self.reports.sweep_checklists()
        self.assertEqual(self.checklists(thread), [])

    async def test_startup_work_runs_once_across_reconnects(self):
        with mock.patch.object(self.reports, "_startup", new=mock.AsyncMock()) as startup:
            await self.reports.on_ready()
            await self.reports.on_ready()
            await asyncio.sleep(0)
        self.assertEqual(startup.call_count, 1)
        self.reports._started = False


class GatherTest(ReviewTest):
    def stored(self, content: str, **extra: Any) -> dict:
        return {"id": new_id(), "channel_id": int(self.text["id"]), "author_id": self.member_id, "content": content, "attachments": [], "created_at": time.time(), "deleted_at": None, "edited_at": None, **extra}

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.store = StubLogging()
        self.bot.modules.append(self.store)

    async def gather(self, thread: dict, first: dict, *values: str) -> None:
        await self.press(REVIEW_START_ID, first, self.staff_id, channel=thread)
        await self.submit_form(self.staff_id, *values, channel=thread)

    async def test_gather_posts_from_the_store_and_moves_to_step_two_without_reading_channels(self):
        thread, first = await self.reviewed()
        self.store.messages = [self.stored(f"message {i}") for i in range(3)]
        self.store.entries = [{"at": time.time(), "event": "delete", "channel_id": int(self.text["id"]), "user_id": self.member_id, "embed": {"title": "Message deleted", "description": "old text"}}]
        scans = len(self.calls_to("GET", "/channels/{channel_id}/messages"))
        await self.gather(thread, first, "3", "1")
        self.assertEqual(len(self.calls_to("GET", "/channels/{channel_id}/messages")), scans)
        embeds = [e for m in self.posts_in(thread["id"]) for e in m["embeds"]]
        texts = [e.get("description") for e in embeds]
        self.assertTrue({"message 0", "message 1", "message 2", "old text"} <= set(texts))
        self.assertEqual((await self.state_of(thread))["current_step"], 1)
        self.assertEqual(self.checklists(thread)[0]["embeds"][0]["title"], "Step 2")
        self.assertFalse(any("Gathering" in m["content"] for m in self.posts_in(thread["id"])))
        self.assert_no_errors()

    async def test_a_message_with_an_image_shows_it_and_other_files_are_linked(self):
        thread, first = await self.reviewed()
        self.store.messages = [self.stored("look", attachments=[
            {"filename": "a.png", "url": "https://cdn.example/a.png", "size": 1, "content_type": "image/png"},
            {"filename": "b.zip", "url": "https://cdn.example/b.zip", "size": 1, "content_type": None},
        ])]
        await self.gather(thread, first, "1", "1")
        embed = next(e for m in self.posts_in(thread["id"]) for e in m["embeds"] if e.get("description") == "look")
        self.assertEqual(embed["image"]["url"], "https://cdn.example/a.png")
        self.assertIn("b.zip", json.dumps(embed["fields"]))
        self.assert_no_errors()

    async def test_a_stored_image_goes_out_without_its_expiring_signature(self):
        thread, first = await self.reviewed()
        signed = "https://cdn.discordapp.com/attachments/1/2/a.png?ex=65d903de&is=65c68ede&hm=abcdef&"
        self.store.messages = [self.stored("look", attachments=[{"filename": "a.png", "url": signed, "size": 1, "content_type": "image/png"}])]
        await self.gather(thread, first, "1", "1")
        embed = next(e for m in self.posts_in(thread["id"]) for e in m["embeds"] if e.get("description") == "look")
        self.assertEqual(embed["image"]["url"], "https://cdn.discordapp.com/attachments/1/2/a.png")

    async def test_only_what_the_store_lacks_falls_back_to_reading_channels(self):
        thread, first = await self.reviewed()
        older = self.net.message_json(int(self.text["id"]), {"content": "from the channel"})
        older["author"] = self.member_json[self.member_id]["user"]
        self.net.messages[int(older["id"])] = older
        newer_than_the_store = self.net.message_json(int(self.text["id"]), {"content": "the store has this already"})
        self.store.messages = [self.stored("from the store")]
        newer_than_the_store["id"] = str(self.store.messages[0]["id"] + 1)
        newer_than_the_store["author"] = self.member_json[self.member_id]["user"]
        self.net.messages[int(newer_than_the_store["id"])] = newer_than_the_store
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "5", "1")
        self.assertTrue(self.calls_to("GET", "/channels/{channel_id}/messages"))
        texts = [e.get("description") for m in self.posts_in(thread["id"]) for e in m["embeds"]]
        self.assertIn("from the store", texts)
        self.assertIn("from the channel", texts)
        self.assertNotIn("the store has this already", texts)
        self.assertFalse(any("Gathering" in m["content"] for m in self.posts_in(thread["id"])), "the progress message comes down")
        self.assert_no_errors()

    async def test_older_log_entries_come_from_the_named_log_channel(self):
        logs = self.log_channel
        await self.configure(log_channel_fallback=int(logs["id"]))
        thread, first = await self.reviewed()
        entry = self.net.message_json(int(logs["id"]), {"embeds": [{"title": "Older entry", "footer": {"text": f"ID: {self.member_id}"}}]})
        other = self.net.message_json(int(logs["id"]), {"embeds": [{"title": "Someone else", "footer": {"text": "ID: 1"}}]})
        self.net.messages[int(entry["id"])] = entry
        self.net.messages[int(other["id"])] = other
        self.store.messages = [self.stored("x")]
        await self.gather(thread, first, "1", "5")
        titles = [e.get("title") for m in self.posts_in(thread["id"]) for e in m["embeds"]]
        self.assertIn("Older entry", titles)
        self.assertNotIn("Someone else", titles)

    async def test_threads_the_member_started_are_listed(self):
        thread, first = await self.reviewed()
        theirs = {"id": str(new_id()), "guild_id": str(self.guild_id), "parent_id": self.text["id"], "owner_id": str(self.member_id), "name": "my thread", "type": 11, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}, "flags": 0}
        self.state.parse_thread_create({**theirs, "newly_created": True})
        self.store.messages = [self.stored("x")]
        await self.gather(thread, first, "1", "1")
        self.assertTrue(any("my thread" in m["content"] for m in self.posts_in(thread["id"])))

    async def test_the_form_refuses_what_isnt_a_small_positive_number(self):
        thread, first = await self.reviewed()
        for bad, expected in (("abc", "whole number"), ("0", "between 1"), ("9999", "between 1")):
            await self.gather(thread, first, bad, "1")
            self.assertIn(expected, self.last_answer())
        self.assertEqual((await self.state_of(thread))["current_step"], 0)
        self.assert_no_errors()

    async def test_a_form_sent_after_the_review_moved_on_does_nothing(self):
        thread, first = await self.reviewed()
        await self.press(REVIEW_START_ID, first, self.staff_id, channel=thread)
        await self.click_done(thread)
        await self.submit_form(self.staff_id, "1", "1", channel=thread)
        self.assertIn("moved past", self.last_answer())
        self.assertEqual((await self.state_of(thread))["current_step"], 1)

    async def test_a_gather_running_is_left_alone_by_reconcile(self):
        thread, first = await self.reviewed()
        self.net.messages.pop(int(first["id"]))
        self.reports._gathering.add(int(thread["id"]))
        await self.reports.reconcile()
        self.assertEqual(self.checklists(thread), [])
        self.reports._gathering.discard(int(thread["id"]))
        await self.reports.reconcile()
        self.assertEqual(len(self.checklists(thread)), 1)

    async def test_no_store_at_all_still_gathers_by_reading_channels(self):
        self.bot.modules.remove(self.store)
        thread, first = await self.reviewed()
        channel_message = self.net.message_json(int(self.text["id"]), {"content": "only in the channel"})
        channel_message["author"] = self.member_json[self.member_id]["user"]
        self.net.messages[int(channel_message["id"])] = channel_message
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.gather(thread, first, "1", "1")
        self.assertIn("only in the channel", [e.get("description") for m in self.posts_in(thread["id"]) for e in m["embeds"]])
        self.assert_no_errors()


class LadderTest(ReportTest):
    def test_default_ladder_recommends_what_the_old_rules_did(self):
        cases = [
            ({}, "Warning"),
            ({"warn": 1}, "24-hour mute"),
            ({"warn": 5}, "24-hour mute"),
            ({"mute": 1}, "3-day mute"),
            ({"mute": 2}, "7-day temp ban"),
            ({"mute": 5, "warn": 1}, "7-day temp ban"),
            ({"tempban": 1}, "Permanent ban"),
            ({"ban": 1}, "Permanent ban"),
        ]
        for counts, expected in cases:
            self.assertEqual(ladder_label(DEFAULT_LADDER[recommended_step(DEFAULT_LADDER, counts)]), expected, counts)

    def test_a_server_can_edit_the_ladder(self):
        ladder = [{"action": "timeout", "seconds": 3600}, {"action": "kick", "seconds": 0}]
        self.assertEqual(recommended_step(ladder, {}), 0)
        self.assertEqual(recommended_step(ladder, {"timeout": 1}), 1)
        self.assertEqual(recommended_step(ladder, {"timeout": 1, "kick": 9}), 1)
        text = propose_action(ladder, {}, "<@1>")
        self.assertIn("1-hour timeout", text)
        self.assertIn("/timeout member:<@1> duration:1h", text)

    def test_empty_ladder_says_so(self):
        self.assertIsNone(recommended_step([], {}))
        self.assertIn("No escalation ladder", propose_action([], {}, "<@1>"))

    async def test_ladder_settings_are_checked(self):
        client = await self.client()
        good = await client.put(self.path("/settings/reports"), json={"ladder": [{"action": "warn"}, {"action": "tempban", "seconds": 3600}]})
        self.assertEqual(good.status, 200)
        self.assertEqual(self.reports.cfg(self.guild_id)["ladder"][1], {"action": "tempban", "seconds": 3600})
        for bad in ([{"action": "explode"}], [{"action": "tempban"}], [{"action": "timeout", "seconds": 99999999}], "nope", [{"action": "warn"}] * 13):
            response = await client.put(self.path("/settings/reports"), json={"ladder": bad})
            self.assertEqual(response.status, 400, bad)


class NoticeTest(ReportTest):
    def notice(self, **overrides: Any) -> str:
        values = {"mention": "<@1>", "punishment": "mute", "duration": "24-hour", "rules": "1, 3", "recap": "1: bans spam\n3: bans ads", "notes": "", "rules_link": "https://example.com/rules"}
        return build_notice(DEFAULT_NOTICE, **{**values, **overrides})

    def test_each_punishment_says_the_right_thing(self):
        self.assertIn("apply a 24-hour mute to your account", self.notice())
        self.assertIn("the mute will remain in place", self.notice())
        self.assertIn("apply a 7-day temporary ban", self.notice(punishment="tempban", duration="7-day"))
        self.assertIn("apply an indefinite mute", self.notice(duration="indefinite"))
        self.assertIn("issue a formal warning", self.notice(punishment="warning"))
        self.assertIn("apply a permanent ban", self.notice(punishment="permban"))
        self.assertIn("remove you from the server", self.notice(punishment="kick"))
        self.assertIn("this ticket will remain open", self.notice(punishment="kick"))
        self.assertIn("apply a mute to your account", self.notice(duration="na"))

    def test_rules_and_recap_read_as_sentences(self):
        self.assertEqual(format_rules_phrase("3"), "rule 3")
        self.assertEqual(format_rules_phrase("1, 3"), "rules 1 and 3")
        self.assertEqual(format_rules_phrase("1 3 5"), "rules 1, 3, and 5")
        self.assertEqual(format_rules_phrase("spam"), "rule spam")
        self.assertEqual(format_recap("3: prohibits spam"), "To recap, Rule 3 prohibits spam.")
        self.assertEqual(format_recap("3: prohibits spam\n5 - enforces respect."), "To recap, Rule 3 prohibits spam, and Rule 5 enforces respect.")
        self.assertEqual(format_recap(""), "")

    def test_the_sentence_about_the_link_goes_with_the_link(self):
        self.assertIn("the rules are linked below.\nhttps://example.com/rules", self.notice())
        without = self.notice(rules_link="")
        self.assertNotIn("linked below", without)
        self.assertIn("read through our community rules", without)

    def test_empty_parts_drop_out_whole(self):
        text = self.notice(recap="", notes="", rules_link="")
        self.assertNotIn("{", text)
        self.assertNotIn("Link to rules", text)
        self.assertNotIn("To recap", text)
        self.assertNotIn("\n\n\n", text)

    def test_notes_and_link_appear_when_given(self):
        text = self.notice(notes="Second time this month.")
        self.assertIn("Second time this month.", text)
        self.assertIn("https://example.com/rules", text)

    def test_a_stray_brace_in_a_servers_template_cant_break_it(self):
        self.assertEqual(build_notice("Hi {user} {nonsense} {", mention="<@1>", punishment="kick", duration="na", rules="1", recap="", notes="", rules_link=""), "Hi <@1> {nonsense} {")

    def test_long_text_splits_between_paragraphs(self):
        pieces = in_pieces(["a" * 1500, "b" * 1500, "c" * 2500], joiner="\n\n")
        self.assertTrue(all(len(p) <= 2000 for p in pieces))
        self.assertEqual("".join(pieces).count("a"), 1500)
        self.assertEqual("".join(pieces).count("c"), 2500)

    async def test_the_form_previews_then_sends_only_when_asked_pinging_only_the_member(self):
        await self.slash("punishment_notice", self.staff_id, user=self.guild.get_member(self.member_id))
        modal = self.net.modals[-1]
        self.assertEqual(modal["title"], "Punishment notice")
        await self.submit_form(self.staff_id, "1, 3", "1: bans spam", "", selects={0: "tempban", 1: "7-day"})
        preview = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertTrue(self.is_private(preview))
        self.assertIn("7-day temporary ban", preview["embeds"][0]["description"])
        sent_before = len(self.posts_in(self.text["id"]))
        message = self.net.posted[-1]
        self.assertEqual(len(self.posts_in(self.text["id"])), sent_before)
        send = next(c["custom_id"] for r in message["components"] for c in r["components"] if c["label"] == "Send to this channel")
        await self.press(send, message, self.staff_id)
        body = self.calls_to("POST", "/channels/{channel_id}/messages")[-1][2]
        self.assertIn(f"<@{self.member_id}>", body["content"])
        self.assertEqual(body["allowed_mentions"]["users"], [self.member_id])
        self.assertEqual(body["allowed_mentions"]["parse"], [])
        self.assert_no_errors()

    async def test_cancel_posts_nothing(self):
        await self.slash("punishment_notice", self.staff_id, user=self.guild.get_member(self.member_id))
        await self.submit_form(self.staff_id, "1", "", "")
        mark = len(self.calls_to("POST", "/channels/{channel_id}/messages"))
        message = self.net.posted[-1]
        cancel = next(c["custom_id"] for r in message["components"] for c in r["components"] if c["label"] == "Cancel")
        await self.press(cancel, message, self.staff_id)
        self.assertEqual(len(self.calls_to("POST", "/channels/{channel_id}/messages")), mark)
        self.assertIn("Canceled", [a for a in self.answers() if a.get("content")][-1]["content"])

    async def test_the_servers_own_template_and_link_are_used(self):
        await self.configure(notice_template="Dear {user}, {action}.\n\nSee {rules_link}", notice_rules_link="https://example.com/r")
        await self.slash("punishment_notice", self.staff_id, user=self.guild.get_member(self.member_id))
        await self.submit_form(self.staff_id, "1", "", "", selects={0: "kick"})
        preview = [a for a in self.answers() if a.get("embeds")][-1]["embeds"][0]["description"]
        self.assertEqual(preview, f"Dear <@{self.member_id}>, remove you from the server.\n\nSee https://example.com/r")

    async def test_only_staff_can_write_one(self):
        await self.slash("punishment_notice", self.member_id, user=self.guild.get_member(self.admin_id))
        self.assertIn("Only staff", self.last_answer())
        self.assertEqual(self.net.modals, [])

    async def test_a_text_command_gets_a_button_that_only_its_author_can_press(self):
        self.dispatch_message(user_id=self.staff_id, content=f"!punishment_notice <@{self.member_id}>")
        await self.settle()
        prompt = self.net.posted[-1]
        button = prompt["components"][0]["components"][0]["custom_id"]
        await self.press(button, prompt, self.member_id)
        self.assertEqual(self.net.modals, [])
        await self.press(button, prompt, self.staff_id)
        self.assertEqual(self.net.modals[-1]["title"], "Punishment notice")
        self.assertNotIn(int(prompt["id"]), self.net.messages, "the prompt doesn't stay behind")


class DeleteMessagesTest(ReportTest):
    made = 0

    def chat(self, user_id: int, content: str, *, old: bool = False, **extra: Any) -> dict:
        DeleteMessagesTest.made += 1
        message_id = new_id() if old else discord.utils.time_snowflake(discord.utils.utcnow()) + DeleteMessagesTest.made
        message = self.net.message_json(int(self.text["id"]), {"content": content}, message_id=message_id)
        message["author"] = self.member_json[user_id]["user"]
        message.update(extra)
        self.net.messages[int(message["id"])] = message
        return message

    def gone(self, *messages: dict) -> bool:
        return all(int(m["id"]) not in self.net.messages for m in messages)

    def kept(self, *messages: dict) -> bool:
        return all(int(m["id"]) in self.net.messages for m in messages)

    async def run_delete(self, **options: Any) -> None:
        await self.slash("delete_messages", self.admin_id, **options)

    async def test_no_filter_is_refused_so_a_channel_cant_be_emptied_by_a_slip(self):
        message = self.chat(self.member_id, "hello")
        await self.run_delete()
        self.assertIn("at least one filter", self.last_answer())
        self.assertTrue(self.kept(message))

    async def test_user_filter_takes_a_mention_or_an_id_and_works_for_people_who_left(self):
        mine = self.chat(self.member_id, "hello")
        other = self.chat(self.mod_id, "hi")
        await self.run_delete(user=f"<@{self.member_id}>")
        self.assertTrue(self.gone(mine))
        self.assertTrue(self.kept(other))
        former = self.chat(self.mod_id, "from someone who left")
        former["author"] = user_json(new_id(), "gone")
        await self.run_delete(user=former["author"]["id"])
        self.assertTrue(self.gone(former))

    async def test_bad_user_and_bad_date_are_refused(self):
        await self.run_delete(user="someone")
        self.assertIn("mention or a numeric ID", self.last_answer())
        await self.run_delete(until_date="05/11")
        self.assertIn("2026-11-05", self.last_answer())
        self.assert_no_errors()

    async def test_plain_keyword_is_case_sensitive_and_reads_embeds(self):
        hit = self.chat(self.member_id, "buy Cheap stuff")
        miss = self.chat(self.member_id, "buy cheap stuff")
        embedded = self.chat(self.mod_id, "", embeds=[{"description": "very Cheap", "type": "rich"}])
        field = self.chat(self.mod_id, "", embeds=[{"fields": [{"name": "Offer", "value": "Cheap", "inline": False}], "type": "rich"}])
        await self.run_delete(keyword="Cheap")
        self.assertTrue(self.gone(hit, embedded, field))
        self.assertTrue(self.kept(miss))
        self.assertTrue(self.calls_to("POST", "/channels/{channel_id}/messages/bulk-delete"), "recent messages go in bulk")

    async def test_regex_keyword_runs_in_its_own_process(self):
        hit = self.chat(self.member_id, "order 123 now")
        miss = self.chat(self.member_id, "no number here")
        await self.run_delete(keyword=r"re:\d{3}")
        self.assertTrue(self.gone(hit))
        self.assertTrue(self.kept(miss))
        self.assert_no_errors()

    async def test_a_regex_that_doesnt_compile_is_refused_before_anything_runs(self):
        message = self.chat(self.member_id, "x")
        await self.run_delete(keyword="re:(unclosed")
        self.assertIn("isn't a valid pattern", self.last_answer())
        self.assertTrue(self.kept(message))

    async def test_a_regex_that_backtracks_forever_stops_the_job_and_the_bot_keeps_running(self):
        message = self.chat(self.member_id, "a" * 64 + "!")
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        beat = asyncio.ensure_future(heartbeat())
        await self.run_delete(keyword="re:(a+)+$")
        beat.cancel()
        self.assertIn("took too long", self.last_answer())
        self.assertTrue(self.kept(message))
        self.assertGreater(ticks, 5, "the event loop kept running during the match")

    async def test_gifs_only(self):
        gif_file = self.chat(self.member_id, "", attachments=[{"id": str(new_id()), "filename": "a.gif", "size": 1, "url": "https://cdn.example/a.gif", "proxy_url": "https://cdn.example/a.gif", "content_type": "image/gif"}])
        tenor = self.chat(self.member_id, "https://tenor.com/view/x")
        plain = self.chat(self.member_id, "words")
        await self.run_delete(gifs_only=True)
        self.assertTrue(self.gone(gif_file, tenor))
        self.assertTrue(self.kept(plain))

    async def test_count_limits_what_is_looked_through_even_when_nothing_matches(self):
        target = self.chat(self.member_id, "oldest")
        for _ in range(5):
            self.chat(self.mod_id, "newer")
        await self.run_delete(user=str(self.member_id), count=5)
        self.assertIn("Looked through 5", self.last_answer())
        self.assertTrue(self.kept(target))

    async def test_one_private_message_says_how_it_went(self):
        self.chat(self.member_id, "hello")
        await self.run_delete(keyword="hello")
        notes = [a["content"] for a in self.answers() if a.get("content")]
        self.assertEqual(len(notes), 2, notes)
        self.assertEqual(notes[0], "Looking through messages...")
        self.assertRegex(notes[1], r"^Finished\. Looked through \d+ messages? and deleted 1\.$")
        self.assertTrue(self.calls_to("PATCH", "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"), "the same message, edited")

    async def test_until_date_stops_there(self):
        await self.run_delete(until_date="2020-01-01", keyword="zzz")
        self.assertIn("Finished", self.last_answer())
        params = [c for c in self.calls_to("GET", "/channels/{channel_id}/messages")]
        self.assertTrue(params)

    async def test_old_messages_are_deleted_one_at_a_time(self):
        old = self.chat(self.member_id, "ancient", old=True)
        await self.run_delete(user=str(self.member_id))
        self.assertTrue(self.gone(old))
        self.assertFalse(self.calls_to("POST", "/channels/{channel_id}/messages/bulk-delete"))
        self.assertTrue(self.calls_to("DELETE", "/channels/{channel_id}/messages/{message_id}"))

    async def test_needs_administrator_or_a_bypass_role(self):
        await self.slash("delete_messages", self.mod_id, keyword="x")
        self.assertIn("Administrator", self.last_answer())

    async def test_bot_needs_manage_messages_there(self):
        with mock.patch.object(discord.TextChannel, "permissions_for", lambda self_, m: discord.Permissions(view_channel=True)):
            await self.run_delete(keyword="x")
        self.assertIn("Manage Messages", self.last_answer())

    async def test_a_discord_refusal_stops_cleanly(self):
        self.chat(self.member_id, "hello")
        self.chat(self.member_id, "hello again")
        restore = self.failing("POST", "/channels/{channel_id}/messages/bulk-delete", 403)
        await self.run_delete(keyword="hello")
        restore()
        self.assertIn("Stopped", self.last_answer())
        self.assert_no_errors()

    async def test_the_flags_work_as_text_too(self):
        hit = self.chat(self.member_id, "buy now")
        miss = self.chat(self.member_id, "hello")
        command = self.chat(self.admin_id, f"!delete_messages user: {self.member_id} keyword: buy", guild_id=str(self.guild_id), member=self.member_json[self.admin_id])  # newer than what it looks through, as a real message's ID is
        self.state.parse_message_create(command)
        await self.settle()
        self.assertTrue(self.gone(hit))
        self.assertTrue(self.kept(miss))
        self.assert_no_errors()

    def test_parse_user_id(self):
        self.assertEqual(parse_user_id("<@123>"), 123)
        self.assertEqual(parse_user_id("<@!123>"), 123)
        self.assertEqual(parse_user_id(" 123 "), 123)
        self.assertIsNone(parse_user_id("bob"))


class RoleToolsTest(ReportTest):
    PACK = [{"name": "Zebra Fan", "emoji": "\N{ZEBRA FACE}"}, {"name": "apple Fan", "emoji": "\N{RED APPLE}"}]

    async def test_role_order_lists_top_to_bottom_privately(self):
        await self.slash("role_order", self.admin_id)
        answer = [a for a in self.answers() if a.get("content") and "```" in a["content"]][-1]
        self.assertTrue(self.is_private(answer))
        self.assertLess(answer["content"].index("Captain Guild"), answer["content"].index("@everyone"))

    async def test_role_order_needs_administrator(self):
        await self.slash("role_order", self.mod_id)
        self.assertIn("Administrator", self.last_answer())

    async def test_empty_pack_says_where_to_fill_it(self):
        await self.slash("deploy_fun_roles", self.admin_id, anchor=self.guild.get_role(int(self.role_member["id"])), style="prefix", dry_run=False)
        self.assertIn("empty", self.last_answer())
        self.assertFalse(self.calls_to("POST", "/guilds/{guild_id}/roles"))

    async def test_dry_run_changes_nothing_and_is_the_default(self):
        await self.configure(fun_roles=self.PACK)
        await self.slash("deploy_fun_roles", self.admin_id, anchor=self.guild.get_role(int(self.role_member["id"])), style="prefix")
        self.assertIn("Dry run", self.last_answer())
        self.assertLess(self.last_answer().index("apple Fan"), self.last_answer().index("Zebra Fan"))
        self.assertFalse(self.calls_to("POST", "/guilds/{guild_id}/roles"))
        self.assertFalse(self.calls_to("PATCH", "/guilds/{guild_id}/roles"))

    async def test_deploy_creates_what_is_missing_and_slots_the_block_below_the_anchor(self):
        await self.configure(fun_roles=self.PACK)
        made: dict[str, dict] = {}
        real = self.bot.http.request

        def renumber(*bottom_up: dict) -> None:
            for position, role in enumerate(bottom_up, 1):
                role["position"] = position
                self.state.parse_guild_role_update({"guild_id": str(self.guild_id), "role": role})

        ladder = [self.role_muted, self.alert, self.role_member, self.role_mod, self.moderator_role, self.role_admin, self.role_bot]
        renumber(*ladder)

        async def creating(route: Any, **kwargs: Any) -> Any:
            if route.method == "POST" and route.path == "/guilds/{guild_id}/roles":
                role = await real(route, **kwargs)
                self.net.roles[int(role["id"])] = role
                made[role["name"]] = role
                ladder.insert(0, role)  # Discord puts a new role just above @everyone
                self.state.parse_guild_role_create({"guild_id": str(self.guild_id), "role": role})
                renumber(*ladder)
                return role
            return await real(route, **kwargs)
        self.bot.http.request = creating
        anchor = self.guild.get_role(int(self.role_mod["id"]))
        with mock.patch("captainguild.modules.reports.ROLE_PAUSE", 0):
            await self.slash("deploy_fun_roles", self.admin_id, anchor=anchor, style="prefix", dry_run=False)
        self.bot.http.request = real
        self.assertEqual(sorted(made), ["\N{RED APPLE} apple Fan", "\N{ZEBRA FACE} Zebra Fan"])
        moved = {int(p["id"]): p["position"] for p in self.calls_to("PATCH", "/guilds/{guild_id}/roles")[-1][2]}
        final = {int(r["id"]): r["position"] for r in ladder}
        final.update(moved)
        apple, zebra = int(made["\N{RED APPLE} apple Fan"]["id"]), int(made["\N{ZEBRA FACE} Zebra Fan"]["id"])
        anchor_at = final[int(self.role_mod["id"])]
        self.assertEqual(final[apple], anchor_at - 1, "A sits directly below the anchor")
        self.assertEqual(final[zebra], anchor_at - 2, "and Z below that")
        self.assertLess(final[int(self.role_member["id"])], final[zebra], "roles that were below the anchor stay below the block")
        self.assertNotIn(int(self.moderator_role["id"]), moved, "nothing above the anchor moves")
        self.assertNotIn(int(self.role_bot["id"]), moved)
        self.assertIn("Created 2", self.last_answer())
        self.assert_no_errors()

    async def test_icon_style_sends_the_emoji_as_the_icon_where_the_server_has_them(self):
        await self.configure(fun_roles=self.PACK)
        self.guild.features.append("ROLE_ICONS")
        with mock.patch("captainguild.modules.reports.ROLE_PAUSE", 0):
            await self.slash("deploy_fun_roles", self.admin_id, anchor=self.guild.get_role(int(self.role_member["id"])), style="icon", dry_run=False)
        bodies = [c[2] for c in self.calls_to("POST", "/guilds/{guild_id}/roles")]
        self.assertEqual(sorted(b["name"] for b in bodies), ["Zebra Fan", "apple Fan"])
        self.assertIn("\N{RED APPLE}", [b.get("unicode_emoji") for b in bodies])

    async def test_icon_style_needs_role_icons(self):
        await self.configure(fun_roles=self.PACK)
        await self.slash("deploy_fun_roles", self.admin_id, anchor=self.guild.get_role(int(self.role_member["id"])), style="icon")
        self.assertIn("role icons", self.last_answer())

    async def test_deploy_refuses_everyone_and_an_anchor_above_the_bot(self):
        await self.configure(fun_roles=self.PACK)
        high = self.add_role("Way Up", 50)
        await self.slash("deploy_fun_roles", self.admin_id, anchor=self.guild.get_role(int(high["id"])), style="prefix")
        self.assertIn("must be above", self.last_answer())
        await self.slash("deploy_fun_roles", self.admin_id, anchor=self.guild.default_role, style="prefix")
        self.assertIn("@everyone", self.last_answer())

    async def test_delete_has_a_dry_run_and_skips_what_it_cant_touch(self):
        await self.configure(fun_roles=[{"name": "Member", "emoji": ""}, {"name": "Admin", "emoji": ""}, {"name": "Captain Guild", "emoji": ""}])
        await self.slash("delete_fun_roles", self.admin_id)
        self.assertIn("Dry run", self.last_answer())
        self.assertFalse(self.calls_to("DELETE", "/guilds/{guild_id}/roles/{role_id}"))
        with mock.patch("captainguild.modules.reports.ROLE_PAUSE", 0):
            await self.slash("delete_fun_roles", self.admin_id, dry_run=False)
        self.assertEqual(len(self.calls_to("DELETE", "/guilds/{guild_id}/roles/{role_id}")), 2)
        self.assertIn("skipped", self.last_answer())

    async def test_list_shows_the_pack(self):
        await self.configure(fun_roles=self.PACK)
        await self.slash("fun_roles", self.admin_id, "list")
        self.assertIn("Zebra Fan", self.last_answer())


class PingCountTest(ReportTest):
    async def count_of(self, user_id: int) -> int:
        return await self.db.scalar("SELECT count FROM moderator_pings WHERE guild_id = ? AND user_id = ?", (self.guild_id, user_id)) or 0

    async def test_plain_text_counts_for_members_only(self):
        self.dispatch_message(user_id=self.member_id, content="hey @Moderator help")
        self.dispatch_message(user_id=self.member_id, content="no ping here")
        self.dispatch_message(user_id=self.admin_id, content="@moderator from staff")
        await self.settle()
        await asyncio.sleep(0.05)
        self.assertEqual(await self.count_of(self.member_id), 1)
        self.assertEqual(await self.count_of(self.admin_id), 0)

    async def test_a_real_ping_of_the_moderator_role_counts_too(self):
        self.dispatch_message(user_id=self.member_id, content=f"<@&{self.moderator_role['id']}>", mention_roles=[self.moderator_role["id"]])
        await self.settle()
        await asyncio.sleep(0.05)
        self.assertEqual(await self.count_of(self.member_id), 1)

    async def test_the_word_is_a_setting(self):
        await self.configure(mod_ping_text="!staff")
        self.dispatch_message(user_id=self.member_id, content="@moderator")
        self.dispatch_message(user_id=self.member_id, content="calling !STAFF now")
        await self.settle()
        await asyncio.sleep(0.05)
        self.assertEqual(await self.count_of(self.member_id), 1)

    async def test_it_shows_in_the_stats(self):
        await self.configure(forum_channel=int(self.forum["id"]))
        self.dispatch_message(user_id=self.member_id, content="@moderator")
        await self.settle()
        await asyncio.sleep(0.05)
        text = await self.reports._stats_text(self.guild, self.member_id, "member")
        self.assertIn("**Moderator Ping Count:** 1", text)


class WithModerationTest(Harness):
    """The real moderation module on the other end of the interface."""
    modules = [Moderation, Reports]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.reports = self.bot.module("reports")
        self.moderation = self.bot.module("moderation")
        await self.bot.settings.save(self.guild_id, "reports", {"report_channel": int(self.log_channel["id"]), "forum_channel": int(self.forum["id"])})

    async def test_stats_and_recommendation_read_real_cases(self):
        member = self.guild.get_member(self.member_id)
        mod = self.guild.get_member(self.admin_id)
        await self.moderation.record_case(self.guild, "warn", member, mod, reason="first")
        await self.moderation.record_case(self.guild, "mute", member, mod, reason="second", seconds=3600)
        text = await self.reports._stats_text(self.guild, self.member_id, "member")
        self.assertIn("**Warn Count:** 1", text)
        self.assertIn("**Mute Count:** 1", text)
        self.assertIn("Duration: 1 hours", text)
        record = await self.reports._member_record(self.guild_id, self.member_id)
        self.assertIn("3-day mute", propose_action(self.reports.cfg(self.guild_id)["ladder"], record["counts"], "<@1>"))

    async def test_actioned_links_the_case_made_since_the_report(self):
        await self.user_menu("Report User", self.mod_id, self.member_id)
        await self.submit_form(self.mod_id, "reason")
        row = await self.db.one("SELECT * FROM reports")
        member = self.guild.get_member(self.member_id)
        case = await self.moderation.record_case(self.guild, "warn", member, self.guild.get_member(self.admin_id), reason="for the report")
        fresh = await self.reports.change_state(row, ACTIONED, self.guild.get_member(self.admin_id))
        self.assertEqual(fresh["case_number"], case["number"])
        self.assertIn(f"Linked case: `{case['number']}`", full_report_text(fresh))

    async def test_actioned_links_the_case_not_the_undoing_of_it(self):
        await self.user_menu("Report User", self.mod_id, self.member_id)
        await self.submit_form(self.mod_id, "reason")
        row = await self.db.one("SELECT * FROM reports")
        member = self.guild.get_member(self.member_id)
        mute = await self.moderation.record_case(self.guild, "mute", member, self.guild.get_member(self.admin_id), reason="for the report", seconds=60)
        await self.moderation.record_case(self.guild, "unmute", member, None, reason="It ran out", source="timer")
        fresh = await self.reports.change_state(row, ACTIONED, self.guild.get_member(self.admin_id))
        self.assertEqual(fresh["case_number"], mute["number"])

    async def test_a_case_from_before_the_report_isnt_linked(self):
        member = self.guild.get_member(self.member_id)
        await self.moderation.record_case(self.guild, "warn", member, self.guild.get_member(self.admin_id), reason="old")
        await self.user_menu("Report User", self.mod_id, self.member_id)
        await self.submit_form(self.mod_id, "reason")
        row = await self.db.one("SELECT * FROM reports")
        fresh = await self.reports.change_state(row, ACTIONED, self.guild.get_member(self.admin_id))
        self.assertIsNone(fresh["case_number"])

    async def test_dashboard_case_link_checks_the_member_has_that_case(self):
        await self.user_menu("Report User", self.mod_id, self.member_id)
        await self.submit_form(self.mod_id, "reason")
        row = await self.db.one("SELECT * FROM reports")
        member = self.guild.get_member(self.member_id)
        case = await self.moderation.record_case(self.guild, "warn", member, self.guild.get_member(self.admin_id), reason="x")
        other = await self.moderation.record_case(self.guild, "warn", self.guild.get_member(self.mod_id), self.guild.get_member(self.admin_id), reason="y")
        client = await self.client(access="staff")
        response = await client.post(self.path(f"/reports/{row['id']}/case"), json={"case": other["number"]})
        self.assertEqual(response.status, 400)
        response = await client.post(self.path(f"/reports/{row['id']}/case"), json={"case": case["number"]})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["case_number"], case["number"])
        detail = await (await client.get(self.path(f"/reports/{row['id']}"))).json()
        self.assertEqual([c["number"] for c in detail["cases"]], [case["number"]])
        self.assertTrue(detail["has_moderation"])
        response = await client.post(self.path(f"/reports/{row['id']}/case"), json={"case": None})
        self.assertIsNone((await response.json())["case_number"])


class WithLoggingTest(Harness):
    """The real logging module on the other end of the interface."""
    modules = [Logs, Reports]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.reports = self.bot.module("reports")
        self.store = self.bot.module("logging")
        await self.bot.settings.save(self.guild_id, "reports", {"report_channel": int(self.log_channel["id"]), "forum_channel": int(self.forum["id"])})
        await self.bot.settings.save(self.guild_id, "logging", {"message_channel": int(self.log_channel["id"])})

    async def file_on_member(self) -> tuple[dict, dict]:
        await self.user_menu("Report User", self.mod_id, self.member_id)
        await self.submit_form(self.mod_id, "reason")
        thread = next(c for c in self.net.channels.values() if "thread_metadata" in c)
        checklist = next(m for m in self.posts_in(thread["id"]) if m["embeds"] and m["embeds"][0].get("title") == "Step 1")
        return thread, checklist

    async def test_the_history_button_reads_the_real_store(self):
        self.dispatch_message(user_id=self.member_id, content="something I said earlier")
        await self.settle()
        await self.user_menu("Report User", self.mod_id, self.member_id)
        await self.submit_form(self.mod_id, "reason")
        post = [m for m in self.posts_in(self.log_channel["id"]) if m["embeds"]][-1]
        await self.press(REPORT_HISTORY_ID, post, self.owner_id)
        answer = [a for a in self.answers() if a.get("embeds")][-1]
        self.assertIn("something I said earlier", answer["embeds"][0]["description"])
        self.assert_no_errors()

    async def test_gathering_reads_the_real_store_and_log_entries_without_reading_channels(self):
        kept = self.dispatch_message(user_id=self.member_id, content="this one stays")
        gone = self.dispatch_message(user_id=self.member_id, content="this one gets deleted")
        await self.settle()
        await self.store.flush()
        self.state.parse_message_delete({"id": gone["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        await self.settle()
        await asyncio.sleep(2.5)  # the logging module waits this long for the audit log to name who deleted it
        await self.settle()
        await self.store.flush()
        thread, checklist = await self.file_on_member()
        scans = len(self.calls_to("GET", "/channels/{channel_id}/messages"))
        await self.press(REVIEW_START_ID, checklist, self.admin_id, channel=thread)
        await self.submit_form(self.admin_id, "2", "5", channel=thread)
        embeds = [e for m in self.posts_in(thread["id"]) for e in m["embeds"]]
        texts = [e.get("description") or "" for e in embeds]
        self.assertIn("this one stays", texts)
        self.assertIn("this one gets deleted", texts)
        deleted_copy = next(e for e in embeds if e.get("description") == "this one gets deleted")
        self.assertTrue(any(f["name"] == "Deleted" for f in deleted_copy["fields"]))
        self.assertTrue(any("deleted" in (e.get("title") or "").lower() for e in embeds), "the log entry for the delete")
        self.assertEqual(len(self.calls_to("GET", "/channels/{channel_id}/messages")), scans)
        self.assert_no_errors()

    async def gathered_titles(self, thread: dict, checklist: dict, messages: str, logs: str) -> list[str]:
        with mock.patch("captainguild.modules.reports.SCAN_PAUSE", 0):
            await self.press(REVIEW_START_ID, checklist, self.admin_id, channel=thread)
            await self.submit_form(self.admin_id, messages, logs, channel=thread)
        return [e["title"] for m in self.posts_in(thread["id"]) for e in m["embeds"] if e.get("title") and not e["title"].startswith("Step")]

    async def test_the_log_box_means_message_logs_whatever_else_is_on_record(self):
        now = time.time()
        for index in range(3):
            self.store._store_entry(self.guild_id, "delete", {"title": "Message deleted in #general", "description": f"text {index}"}, at=now - 5000 + index, user_id=self.member_id, channel_id=int(self.text["id"]))
        for index in range(30):
            self.store._store_entry(self.guild_id, "voicejoin", {"title": "Member joined voice channel"}, at=now - 100 + index, user_id=self.member_id)
        thread, checklist = await self.file_on_member()
        titles = await self.gathered_titles(thread, checklist, "1", "5")
        self.assertEqual(titles.count("Message deleted in #general"), 3)
        self.assertNotIn("Member joined voice channel", titles)
        self.assert_no_errors()

    async def test_what_the_store_gave_isnt_read_back_from_the_log_channel(self):
        fallback = int(self.text["id"])
        await self.bot.settings.save(self.guild_id, "reports", {"log_channel_fallback": fallback})
        now = time.time()
        footer = {"text": f"ID: {self.member_id}"}
        self.store._store_entry(self.guild_id, "delete", {"title": "Stored and posted", "footer": footer}, at=now - 60, user_id=self.member_id, channel_id=fallback)

        def posted(title: str, moment: float) -> None:
            message_id = discord.utils.time_snowflake(datetime.datetime.fromtimestamp(moment, datetime.timezone.utc))
            self.net.messages[message_id] = self.net.message_json(fallback, {"embeds": [{"title": title, "footer": footer}]}, message_id=message_id)
        posted("Stored and posted", now - 58)  # the post goes out a moment after the entry is stored
        posted("From before the store", now - 86400)
        thread, checklist = await self.file_on_member()
        titles = await self.gathered_titles(thread, checklist, "1", "5")
        self.assertEqual(titles.count("Stored and posted"), 1)
        self.assertEqual(titles.count("From before the store"), 1)
        self.assert_no_errors()


class DashboardTest(ReportTest):
    async def seeded(self) -> list[dict]:
        await self.report_message(self.target_message("first thing"), reason="spam")
        self.forget_cooldowns()
        await self.user_menu("Report User", self.mod_id, self.admin_id)
        await self.submit_form(self.mod_id, "rude name")
        self.forget_cooldowns()
        await self.report_message(self.target_message("third thing"), by=self.admin_id, reason="ads")
        return await self.rows()

    async def test_lists_newest_first_with_counts_and_pages(self):
        rows = await self.seeded()
        await self.db.run("UPDATE reports SET state = 'actioned' WHERE id = ?", (rows[0]["id"],))
        client = await self.client(access="staff")
        body = await (await client.get(self.path("/reports?size=2"))).json()
        self.assertEqual(body["total"], 3)
        self.assertEqual([r["number"] for r in body["rows"]], [3, 2])
        self.assertEqual(body["counts"], {"open": 2, "actioned": 1, "false_positive": 0})
        second = await (await client.get(self.path("/reports?size=2&page=2"))).json()
        self.assertEqual([r["number"] for r in second["rows"]], [1])
        self.assertIsInstance(body["rows"][0]["target_id"], str, "IDs reach the browser as text")

    async def test_filters_by_state_reporter_target_and_text(self):
        rows = await self.seeded()
        await self.db.run("UPDATE reports SET state = 'false_positive' WHERE id = ?", (rows[1]["id"],))
        client = await self.client(access="staff")

        async def numbers(query: str) -> list[int]:
            return [r["number"] for r in (await (await client.get(self.path(f"/reports?{query}"))).json())["rows"]]
        self.assertEqual(await numbers("state=false_positive"), [2])
        self.assertEqual(await numbers(f"target={self.admin_id}"), [2])
        self.assertEqual(await numbers("target=memb"), [3, 1])
        self.assertEqual(await numbers("reporter=admin"), [3])
        self.assertEqual(await numbers("q=spam"), [1])
        self.assertEqual(await numbers("q=third"), [3])
        self.assertEqual(await numbers("target=100%25"), [])
        response = await client.get(self.path("/reports?state=nonsense"))
        self.assertEqual(response.status, 400)
        response = await client.get(self.path("/reports?page=abc"))
        self.assertEqual(response.status, 400)

    async def test_state_change_updates_the_post_and_records_activity(self):
        rows = await self.seeded()
        client = await self.client(access="staff")
        response = await client.post(self.path(f"/reports/{rows[0]['id']}/state"), json={"state": "actioned"})
        self.assertEqual(response.status, 200)
        post = self.net.messages[rows[0]["post_message_id"]]
        self.assertEqual(post["embeds"][0]["color"], 0x2ECC71)
        self.assertIn("Handled by", post["embeds"][0]["description"])
        by_label = self.buttons(post)
        self.assertEqual(list(by_label), ["Jump to message", "Actioned", "False Positive", "View Message History"])
        self.assertTrue(by_label["Actioned"]["disabled"])
        activity = await self.bot.activity_log.recent(self.guild_id)
        self.assertEqual(activity[0]["action"], "report.state")
        response = await client.post(self.path(f"/reports/{rows[0]['id']}/state"), json={"state": "actioned"})
        self.assertEqual(response.status, 409)
        client = await self.client(access="full")
        response = await client.post(self.path(f"/reports/{rows[0]['id']}/state"), json={"state": "open"})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.buttons(self.net.messages[rows[0]["post_message_id"]])["Actioned"]["disabled"])

    async def test_bad_input_and_other_servers_rows_are_refused(self):
        rows = await self.seeded()
        client = await self.client(access="staff")
        response = await client.post(self.path(f"/reports/{rows[0]['id']}/state"), json={"state": "exploded"})
        self.assertEqual(response.status, 400)
        response = await client.post(self.path("/reports/999999/state"), json={"state": "actioned"})
        self.assertEqual(response.status, 404)
        response = await client.get(self.path("/reports/999999"))
        self.assertEqual(response.status, 404)
        await self.db.run("UPDATE reports SET guild_id = ? WHERE id = ?", (self.guild_id + 1, rows[0]["id"]))
        self.assertEqual((await client.get(self.path(f"/reports/{rows[0]['id']}"))).status, 404)
        self.assertEqual((await client.post(self.path(f"/reports/{rows[0]['id']}/case"), json={"case": 1})).status, 404)
        self.assertEqual((await client.post(self.path(f"/reports/{rows[1]['id']}/case"), json={"case": "abc"})).status, 400)
        listed = await (await client.get(self.path("/reports"))).json()
        self.assertEqual(listed["total"], 2)

    async def test_staff_can_work_reports_but_only_full_access_changes_settings(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/reports"))).status, 200)
        self.assertEqual((await client.get(self.path("/settings/reports"))).status, 200)
        self.assertEqual((await client.put(self.path("/settings/reports"), json={"report_self": False})).status, 403)

    async def test_no_access_means_no_list(self):
        self.bot.ops.access_for = lambda guild_id, user_id: None
        client = await self.client(access="full")
        self.bot.ops.access_for = lambda guild_id, user_id: None
        self.assertEqual((await client.get(self.path("/reports"))).status, 403)

    async def test_threads_tab_lists_reviews_with_their_step(self):
        await self.configure(forum_channel=int(self.forum["id"]))
        await self.report_message(self.target_message())
        self.dispatch_message(user_id=self.member_id, content="@moderator")
        await self.settle()
        await asyncio.sleep(0.05)
        client = await self.client(access="staff")
        body = await (await client.get(self.path("/reports/threads"))).json()
        self.assertEqual(body["total"], 1)
        row = body["rows"][0]
        self.assertEqual((row["status"], row["current_step"], row["pings"]), ("active", 0, 1))
        self.assertIn(row["thread_id"], row["url"])
        self.assertEqual((await (await client.get(self.path("/reports/threads?status=done"))).json())["total"], 0)
        self.assertEqual((await (await client.get(self.path("/reports/threads?member=memb"))).json())["total"], 1)
        self.assertEqual((await client.get(self.path("/reports/threads?status=bad"))).status, 400)
        pings = await (await client.get(self.path("/reports/pings"))).json()
        self.assertEqual(pings[0]["count"], 1)

    async def test_opening_a_review_from_the_dashboard(self):
        await self.configure(forum_channel=int(self.forum["id"]), auto_review=False)
        await self.report_message(self.target_message())
        row = (await self.rows())[0]
        client = await self.client(access="staff")
        response = await client.post(self.path("/reports/review"), json={"user_id": str(self.member_id), "report_id": row["id"]})
        self.assertEqual(response.status, 200)
        self.assertIn("/channels/", (await response.json())["url"])
        thread = next(c for c in self.net.channels.values() if "thread_metadata" in c)
        self.assertTrue(any(m["embeds"] and m["embeds"][0].get("author", {}).get("name") == "Message Report" for m in self.posts_in(thread["id"])))
        response = await client.post(self.path("/reports/review"), json={"user_id": str(self.admin_id), "report_id": row["id"]})
        self.assertEqual(response.status, 404, "a report for someone else can't be attached")
        response = await client.post(self.path("/reports/review"), json={"user_id": "not an id"})
        self.assertEqual(response.status, 400)

    async def test_opening_a_review_with_no_forum_says_so(self):
        await self.configure(forum_channel=None)
        client = await self.client(access="staff")
        response = await client.post(self.path("/reports/review"), json={"user_id": str(self.member_id)})
        self.assertEqual(response.status, 502)
        self.assertIn("No review forum", (await response.json())["error"])

    async def test_settings_page_values_round_trip_with_ids_as_text(self):
        client = await self.client()
        form = {"forum_channel": self.forum["id"], "moderator_role": self.moderator_role["id"], "fun_roles": [{"name": "Pack", "emoji": "\N{RED APPLE}"}], "notice_rules_link": "https://example.com"}
        response = await client.put(self.path("/settings/reports"), json=form)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["forum_channel"], self.forum["id"])
        self.assertEqual(self.reports.cfg(self.guild_id)["forum_channel"], int(self.forum["id"]))
        response = await client.put(self.path("/settings/reports"), json={"fun_roles": [{"emoji": "x"}]})
        self.assertEqual(response.status, 400)
        response = await client.put(self.path("/settings/reports"), json={"cooldown_seconds": 99999})
        self.assertEqual(response.status, 400)


class SmallPiecesTest(ReportTest):
    def test_uid_in_name(self):
        self.assertEqual(uid_in_name("someone UID 123"), 123)
        self.assertIsNone(uid_in_name("someone"))

    def test_has_gif_handles_files_with_no_type(self):
        message = SimpleNamespace(attachments=[SimpleNamespace(content_type=None, filename="file.bin")], content="", embeds=[])
        self.assertFalse(has_gif(message))
