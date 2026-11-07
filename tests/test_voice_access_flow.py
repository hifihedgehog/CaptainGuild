"""Voice access decisions that land together, requests whose post is gone, and limits on what a veteran can undo."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import discord

from captainguild.modules.voice import ACCESS_APPROVE_ID, ACCESS_DENY_ID, Voice
from tests.fakes import BotTest, FakeLogSink
from tests.test_voice import VoiceTest


class Base(VoiceTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.configure(min_messages=0, min_days=0)
        self.asker = self.add_member("asker")

    async def status(self, user_id: int | None = None) -> str | None:
        return await self.db.scalar("SELECT status FROM voice_access WHERE user_id = ?", (user_id or self.asker,))


class RaceTest(Base):
    async def test_an_approve_waiting_on_discord_and_a_deny_cant_both_land(self):
        await self.voice_module.submit_request(self.member(self.asker))
        post = self.posts_in(self.approvals["id"])[-1]
        gate = asyncio.Event()
        real = self.bot.http.request

        async def request(route, **kwargs):
            if route.method == "PUT" and "roles" in route.path:
                await gate.wait()
            return await real(route, **kwargs)
        self.bot.http.request = request
        self.state.parse_interaction_create(self.interaction_payload(3, {"custom_id": ACCESS_APPROVE_ID, "component_type": 2}, user_id=self.staff_id, message=post))
        while await self.status() == "pending":
            await asyncio.sleep(0)
        await self.press(ACCESS_DENY_ID, post, self.owner_id)
        self.assertIn("already resolved", self.last_answer())
        gate.set()
        await self.settle()
        self.assertEqual(await self.status(), "approved")
        self.assert_no_errors()

    async def test_a_denial_landing_while_an_approve_looks_the_member_up_stands(self):
        await self.voice_module.submit_request(self.member(self.asker))
        post = self.posts_in(self.approvals['id'])[-1]
        self.guild._members.pop(self.asker)
        reached, gate = asyncio.Event(), asyncio.Event()
        real = self.bot.http.request

        async def request(route, **kwargs):
            if route.method == 'GET' and route.path.endswith('/members/{member_id}'):
                reached.set()
                await gate.wait()
            return await real(route, **kwargs)
        self.bot.http.request = request
        self.state.parse_interaction_create(self.interaction_payload(3, {'custom_id': ACCESS_APPROVE_ID, 'component_type': 2}, user_id=self.staff_id, message=post))
        await reached.wait()
        await self.press(ACCESS_DENY_ID, post, self.owner_id)
        self.assertEqual(await self.status(), 'denied')
        gate.set()
        await self.settle()
        self.assertEqual(await self.status(), 'denied')
        self.assertEqual(self.role_calls('PUT'), [])
        self.assertIn('already resolved', self.last_answer())
        self.assert_no_errors()

    async def test_a_role_that_wont_go_on_puts_the_request_back(self):
        await self.voice_module.submit_request(self.member(self.asker))
        post = self.posts_in(self.approvals["id"])[-1]
        restore = self.failing("PUT", "/roles/{role_id}", 403)
        await self.press(ACCESS_APPROVE_ID, post, self.staff_id)
        restore()
        self.assertEqual(await self.status(), "pending")
        self.assertIn("can't give", self.last_answer())
        await self.press(ACCESS_APPROVE_ID, post, self.staff_id)
        self.assertEqual(await self.status(), "approved")
        self.assert_no_errors()

    async def test_the_first_answer_goes_out_before_the_role_call(self):
        await self.voice_module.submit_request(self.member(self.asker))
        post = self.posts_in(self.approvals["id"])[-1]
        self.net.calls.clear()
        await self.press(ACCESS_APPROVE_ID, post, self.staff_id)
        order = [call[1] for call in self.net.calls if call[0] != "GET"]
        self.assertTrue(order[0].endswith("/callback"), order)


class LostRequestTest(Base):
    async def test_a_request_whose_post_is_gone_can_be_asked_again(self):
        await self.voice_module.submit_request(self.member(self.asker))
        first = self.posts_in(self.approvals["id"])[-1]
        del self.net.messages[int(first["id"])]
        answer = await self.voice_module.submit_request(self.member(self.asker))
        self.assertIn("went to the moderators", answer)
        self.assertEqual(await self.status(), "pending")
        self.assertEqual(len(self.posts_in(self.approvals["id"])), 1)

    async def test_a_request_that_was_never_posted_can_be_asked_again(self):
        await self.db.run("INSERT INTO voice_access (guild_id, user_id, status, requested_at) VALUES (?, ?, 'pending', ?)", (self.guild_id, self.asker, time.time() - 3600))
        self.assertIn("went to the moderators", await self.voice_module.submit_request(self.member(self.asker)))

    async def test_a_second_click_while_the_first_is_posting_doesnt_post_twice(self):
        await self.db.run("INSERT INTO voice_access (guild_id, user_id, status, requested_at) VALUES (?, ?, 'pending', ?)", (self.guild_id, self.asker, time.time()))
        self.assertIn("already waiting", await self.voice_module.submit_request(self.member(self.asker)))
        self.assertEqual(self.posts_in(self.approvals["id"]), [])

    async def test_a_post_still_there_keeps_the_member_waiting(self):
        await self.voice_module.submit_request(self.member(self.asker))
        self.assertIn("already waiting", await self.voice_module.submit_request(self.member(self.asker)))
        self.assertEqual(len(self.posts_in(self.approvals["id"])), 1)

    async def test_a_decision_elsewhere_closes_the_post_where_it_was_made(self):
        await self.voice_module.submit_request(self.member(self.asker))
        post = self.posts_in(self.approvals["id"])[-1]
        moved = self.add_channel("new-approvals")
        await self.configure(approval_channel=int(moved["id"]))
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.asker))
        self.assertEqual(self.net.messages[int(post["id"])].get("components"), [])
        self.assert_no_errors()


class VeteranLimitsTest(Base):
    async def test_a_veteran_cant_undo_a_revoke_or_a_denial(self):
        for status in ("revoked", "denied"):
            await self.db.run("INSERT INTO voice_access (guild_id, user_id, status) VALUES (?, ?, ?) ON CONFLICT (guild_id, user_id) DO UPDATE SET status = excluded.status", (self.guild_id, self.asker, status))
            await self.slash("vc_access_grant", self.vet_id, user=self.member(self.asker))
            self.assertIn("A moderator closed this one", self.last_answer())
            self.assertEqual(await self.status(), status)
        self.assertEqual(self.role_calls("PUT"), [])

    async def test_a_moderator_can_undo_them(self):
        await self.db.run("INSERT INTO voice_access (guild_id, user_id, status) VALUES (?, ?, 'revoked')", (self.guild_id, self.asker))
        await self.slash("vc_access_grant", self.staff_id, user=self.member(self.asker))
        self.assertEqual(await self.status(), "approved")

    async def test_a_veteran_grant_with_nowhere_to_log_is_refused(self):
        await self.configure(approval_channel=None)
        await self.slash("vc_access_grant", self.vet_id, user=self.member(self.asker))
        self.assertIn("approval channel", self.last_answer())
        self.assertIsNone(await self.status())


class HandRemovalTest(BotTest):
    modules = [Voice]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.logs = FakeLogSink()
        self.voice = self.cog(Voice)
        self.access = self.guild.role("Voice Access", 1)
        self.approvals = self.guild.text_channels[0]
        await self.bot.settings.save(self.guild.id, "voice", {"access_role": self.access.id, "approval_channel": self.approvals.id})
        await self.db.run("INSERT INTO voice_access (guild_id, user_id, status) VALUES (?, ?, 'approved')", (self.guild.id, self.member.id))
        self.member.roles.append(self.access)

    def audit(self, user) -> None:
        self.bot.audit.feed(SimpleNamespace(
            guild=self.guild, action=discord.AuditLogAction.member_role_update, target=self.member, user=user, user_id=user.id, reason="behavior", extra=None,
            changes=SimpleNamespace(before=SimpleNamespace(roles=[self.access]), after=SimpleNamespace(roles=[])), id=len(self.guild.audit_entries) + 1,
        ))

    async def take_off(self) -> None:
        before = SimpleNamespace(pending=False, roles=list(self.member.roles), get_role=lambda role_id: self.access)
        self.member.roles.remove(self.access)
        await self.voice.on_member_update(before, self.member)

    async def status(self) -> str:
        return await self.db.scalar("SELECT status FROM voice_access WHERE user_id = ?", (self.member.id,))

    async def test_a_moderator_taking_the_role_off_makes_it_stick(self):
        self.audit(self.mod)
        await self.take_off()
        self.assertEqual(await self.status(), "revoked")
        self.assertEqual(await self.db.scalar("SELECT decided_by FROM voice_access WHERE user_id = ?", (self.member.id,)), self.mod.id)
        self.assertEqual([embed["title"] for _, embed, _ in self.bot.logs.posted], ["Voice Access Revoked"])
        self.assertIn("denied or revoked", await self.voice.submit_request(self.member))
        self.assertNotIn(self.access, self.member.roles)

    async def test_a_bot_taking_it_off_changes_nothing(self):
        self.audit(self.me)  # a hardmute, or another bot's sync
        await self.take_off()
        self.assertEqual(await self.status(), "approved")
        self.assertEqual(self.bot.logs.posted, [])

    async def test_with_no_audit_entry_nothing_is_assumed(self):
        self.voice._audit_wait = 0
        await self.take_off()
        self.assertEqual(await self.status(), "approved")
