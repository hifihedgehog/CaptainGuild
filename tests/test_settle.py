"""settle() waits for a feature's work to finish. It must return once only finished tasks are left, even before their cleanup runs."""
from __future__ import annotations

import asyncio
from unittest import mock

from captainguild.modules.reactionroles import ReactionRoles
from captainguild.modules.voicelink import VoiceLink
from tests.fakes import BotTest


async def finished_task() -> asyncio.Task:
    async def nothing() -> None:
        return None
    task = asyncio.ensure_future(nothing())
    await task
    return task


class SettleTest(BotTest):
    modules = [ReactionRoles, VoiceLink]

    async def settled(self, settle) -> None:
        calls = 0
        real = asyncio.gather

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 100:
                raise AssertionError("settle() kept waiting on a task that had already finished")
            return real(*args, **kwargs)
        with mock.patch.object(asyncio, "gather", counted):
            await asyncio.wait_for(settle(), 5)

    async def test_reaction_roles_settle_past_a_finished_task_still_listed(self):
        self.cog(ReactionRoles)._background.add(await finished_task())  # finished, before its callback takes it out
        await self.settled(self.cog(ReactionRoles).settle)

    async def test_voice_links_settle_past_a_finished_check_still_listed(self):
        self.cog(VoiceLink)._running[(self.guild.id, self.member.id)] = await finished_task()
        await self.settled(self.cog(VoiceLink).settle)

    async def test_settle_still_waits_for_work_that_is_running(self):
        gate = asyncio.Event()

        async def work() -> None:
            await gate.wait()
        task = asyncio.ensure_future(work())
        self.cog(ReactionRoles)._background.add(task)
        task.add_done_callback(self.cog(ReactionRoles)._background.discard)
        waiting = asyncio.ensure_future(self.cog(ReactionRoles).settle())
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        gate.set()
        await asyncio.wait_for(waiting, 5)
