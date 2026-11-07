import asyncio
import io
import json
import time
from types import SimpleNamespace
from unittest import mock

import aiohttp
import discord
from PIL import Image

from captainguild.core import rankcard
from captainguild.core.ops import DiscordOps
from captainguild.core.outside import OutsideError, check_public_link
from captainguild.core.validate import Invalid
from captainguild.modules.levels import DEFAULT_MESSAGE, MEE6_URL, Levels, LeaderboardView, level_for_xp, parse_rate, xp_for_level, xp_to_next
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, FakeMessage, _FakeResponse, new_id


def png(width: int = 200, height: int = 100, color: tuple = (200, 40, 40), kind: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format=kind)
    return out.getvalue()


class FakeAttachment:
    def __init__(self, data: bytes, *, fail: bool = False) -> None:
        self.data = data
        self.size = len(data)
        self.fail = fail

    async def read(self) -> bytes:
        if self.fail:
            raise discord.NotFound(_FakeResponse(404), "gone")
        return self.data


class CurveTest(BotTest):
    async def test_the_documented_table_lines_up(self):
        # What members know: the XP to finish each level, 100 for level 0, 255 for level 1, and so on to 5,775 for level 10.
        known = [100, 255, 475, 770, 1150, 1625, 2205, 2900, 3720, 4675, 5775]
        for level, total in enumerate(known):
            self.assertEqual(xp_for_level(level + 1), total)

    async def test_it_matches_a_real_mee6_member(self):
        # A published leaderboard row: 1,990,855 XP is level 101, 36,505 into a 56,155 XP level.
        self.assertEqual(level_for_xp(1990855), 101)
        self.assertEqual(1990855 - xp_for_level(101), 36505)
        self.assertEqual(xp_to_next(101), 56155)

    async def test_level_boundaries_are_exact(self):
        self.assertEqual(level_for_xp(0), 0)
        self.assertEqual(level_for_xp(99), 0)
        self.assertEqual(level_for_xp(100), 1)
        for level in (1, 2, 10, 55, 999):
            self.assertEqual(level_for_xp(xp_for_level(level)), level)
            self.assertEqual(level_for_xp(xp_for_level(level) - 1), level - 1)
            self.assertEqual(xp_for_level(level + 1) - xp_for_level(level), xp_to_next(level))

    async def test_rate_text(self):
        self.assertEqual(parse_rate("2/60"), (2, 60))
        self.assertEqual(parse_rate("3 per 120"), (3, 120))
        self.assertEqual(parse_rate("30s"), (1, 30))
        self.assertIsNone(parse_rate("soon"))


class LevelsCase(BotTest):
    modules = [Levels]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cog_ = self.cog(Levels)
        self.now = time.time()
        self.cog_._clock = lambda: self.now
        self.cog_._confirm = mock.AsyncMock(return_value=True)  # test_levels_commands_more covers the question itself

    async def turn_on(self, **settings) -> None:
        await self.bot.settings.save(self.guild.id, "levels", {"enabled": True, **settings})

    async def say(self, author=None, channel=None, **kwargs) -> FakeMessage:
        message = FakeMessage(channel or self.channel, author or self.member, "hello", **kwargs)
        await self.cog_.on_message(message)
        return message

    async def xp_of(self, member=None) -> int:
        await self.cog_.flush()
        return (await self.db.scalar("SELECT xp FROM level_members WHERE guild_id = ? AND user_id = ?", (self.guild.id, (member or self.member).id))) or 0


class EarningTest(LevelsCase):
    async def test_a_message_earns_fifteen_to_twenty_five(self):
        await self.turn_on()
        await self.say()
        self.assertTrue(15 <= await self.xp_of() <= 25)

    async def test_one_award_per_window_then_another_after_it(self):
        await self.turn_on()
        await self.say()
        first = await self.xp_of()
        await self.say()
        await self.say()
        self.assertEqual(await self.xp_of(), first)
        self.now += 61
        await self.say()
        self.assertGreater(await self.xp_of(), first)

    async def test_a_higher_rate_allows_that_many_per_window(self):
        await self.turn_on(rate=2, window=60, custom_xp=10)
        for _ in range(5):
            await self.say()
        self.assertEqual(await self.xp_of(), 20)

    async def test_a_burst_of_simultaneous_messages_earns_once(self):
        await self.turn_on(custom_xp=10)
        await asyncio.gather(*(self.say() for _ in range(10)))
        self.assertEqual(await self.xp_of(), 10)

    async def test_custom_xp_replaces_the_default(self):
        await self.turn_on(custom_xp=42)
        await self.say()
        self.assertEqual(await self.xp_of(), 42)

    async def test_nothing_is_earned_while_off(self):
        await self.say()
        self.assertEqual(await self.xp_of(), 0)

    async def test_bots_and_webhooks_and_system_messages_earn_nothing(self):
        await self.turn_on()
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.say(author=robot)
        self.assertEqual(await self.xp_of(robot), 0)
        message = FakeMessage(self.channel, self.member, "hook")
        message.webhook_id = 5
        await self.cog_.on_message(message)
        message = FakeMessage(self.channel, self.member, "joined")
        message.type = discord.MessageType.new_member
        await self.cog_.on_message(message)
        self.assertEqual(await self.xp_of(), 0)

    async def test_blacklisted_channel_category_thread_and_role_earn_nothing(self):
        category = FakeChannel(self.guild, "quiet", discord.ChannelType.category)
        inside = FakeChannel(self.guild, "inside", category=category)
        blocked = FakeChannel(self.guild, "spam")
        thread = FakeChannel(self.guild, "thread", discord.ChannelType.public_thread)
        thread.parent_id = blocked.id
        await self.turn_on(blacklist_channels=[blocked.id, category.id], blacklist_roles=[self.role_muted.id])
        await self.say(channel=blocked)
        await self.say(channel=inside)
        await self.say(channel=thread)
        muted = FakeMember(self.guild, "muted", self.role_muted)
        await self.say(author=muted)
        self.assertEqual(await self.xp_of(), 0)
        self.assertEqual(await self.xp_of(muted), 0)
        await self.say()
        self.assertGreater(await self.xp_of(), 0)


class BatchingTest(LevelsCase):
    async def test_xp_waits_in_memory_and_goes_out_as_one_write(self):
        await self.turn_on(custom_xp=5)
        others = [FakeMember(self.guild, f"m{i}") for i in range(20)]
        with mock.patch.object(self.db, "many", wraps=self.db.many) as writes:
            for member in others:
                await self.say(author=member)
            self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM level_members"), 0)
            writes.assert_not_called()
            await self.cog_.flush()
            self.assertEqual(writes.call_count, 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM level_members"), 20)

    async def test_a_restart_reads_saved_xp_back(self):
        await self.turn_on(custom_xp=7)
        await self.say()
        await self.cog_.flush()
        fresh = Levels(self.bot)
        fresh._clock = self.cog_._clock
        entry = await fresh._entry(self.guild.id, self.member.id)
        self.assertEqual(entry.xp, 7)
        self.assertEqual(entry.messages, 1)

    async def test_shutdown_saves_what_is_waiting(self):
        await self.turn_on(custom_xp=9)
        await self.say()
        await self.cog_.cog_unload()
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members WHERE user_id = ?", (self.member.id,)), 9)

    async def test_a_failed_write_keeps_the_xp_for_the_next_try(self):
        await self.turn_on(custom_xp=3)
        await self.say()
        with mock.patch.object(self.db, "many", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                await self.cog_.flush()
        self.assertEqual(await self.xp_of(), 3)

    async def test_the_background_save_runs_on_its_own(self):
        await self.turn_on(custom_xp=4)
        with mock.patch("captainguild.modules.levels.FLUSH_SECONDS", 0.01):
            await self.say()
            await asyncio.sleep(0.1)
        self.assertEqual(await self.db.scalar("SELECT xp FROM level_members WHERE user_id = ?", (self.member.id,)), 4)


class LevelUpTest(LevelsCase):
    async def level_up(self, **settings) -> None:
        """Puts the member one message short of level 1, then sends that message."""
        await self.turn_on(custom_xp=10, **settings)
        await self.cog_.set_xp(self.guild.id, self.member.id, 95)
        await self.say()

    async def test_announced_in_the_same_channel_with_variables_filled(self):
        await self.level_up(destination="same", message={"content": "{user} hit {lvl}, {mention}"})
        self.assertEqual(len(self.channel.sent), 1)
        sent = self.channel.sent[0]
        self.assertEqual(sent.content, f"member hit 1, {self.member.mention}")
        mentions = sent.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertTrue(mentions.users)

    async def test_default_message_is_used_out_of_the_box(self):
        await self.level_up(destination="same")
        self.assertIn(self.member.mention, self.channel.sent[0].content)
        self.assertIn("1", self.channel.sent[0].content)

    async def test_an_embed_goes_along(self):
        await self.level_up(destination="same", message={"content": "", "embeds": [{"title": "Level {lvl}"}]})
        self.assertEqual(self.channel.sent[0].embeds[0].title, "Level 1")

    async def test_destination_none_is_silent(self):
        await self.level_up()
        self.assertEqual(self.channel.sent, [])

    async def test_a_chosen_channel_gets_it(self):
        other = FakeChannel(self.guild, "levels")
        await self.level_up(destination=other.id)
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(len(other.sent), 1)

    async def test_dm_destination(self):
        await self.level_up(destination="dm")
        self.assertEqual(len(self.member.dms), 1)

    async def test_a_deleted_destination_does_not_raise(self):
        other = FakeChannel(self.guild, "levels")
        await self.turn_on(custom_xp=10, destination=other.id)
        self.guild.channels.remove(other)
        await self.cog_.set_xp(self.guild.id, self.member.id, 95)
        await self.say()
        self.assertEqual(await self.xp_of(), 105)

    async def test_a_refused_message_does_not_raise(self):
        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.channel.send = refuse
        await self.level_up(destination="same")
        self.assertEqual(await self.xp_of(), 105)

    async def test_it_only_announces_once(self):
        await self.level_up(destination="same")
        self.now += 61
        await self.say()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_modulo_and_minimum_decide_what_is_announced(self):
        await self.turn_on(custom_xp=10, destination="same", modulo=5, minimum=10)
        for level, expected in ((4, 0), (5, 0), (9, 0), (10, 1), (11, 1), (15, 2)):
            self.now += 61
            await self.cog_.set_xp(self.guild.id, self.member.id, xp_for_level(level) - 1)
            await self.say()
            self.assertEqual(len(self.channel.sent), expected, f"level {level}")

    async def test_reward_only_announces_levels_with_a_reward(self):
        await self.turn_on(custom_xp=10, destination="same", only_rewards=True, rewards=[{"level": 2, "role_id": self.role_member.id}])
        await self.cog_.set_xp(self.guild.id, self.member.id, 95)
        await self.say()
        self.assertEqual(self.channel.sent, [])
        self.now += 61
        await self.cog_.set_xp(self.guild.id, self.member.id, 250)
        await self.say()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_the_log_records_level_ups(self):
        await self.level_up()
        rows = await self.db.all("SELECT user_id, level FROM level_events WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual(rows, [{"user_id": self.member.id, "level": 1}])
        ctx = self.context()
        await self.cog_.level_log.callback(self.cog_, ctx)
        self.assertIn("reached level 1", ctx.sent[0]["embed"].description)
        self.assertEqual(ctx.sent[0]["allowed_mentions"].users, False)


class RewardTest(LevelsCase):
    async def reach(self, member, level) -> None:
        self.now += 61
        await self.cog_.set_xp(self.guild.id, member.id, xp_for_level(level) - 1)
        await self.say(author=member)

    async def test_rewards_stack_by_default(self):
        one, two = self.guild.role("One", 3), self.guild.role("Two", 4)
        await self.turn_on(rewards=[{"level": 1, "role_id": one.id}, {"level": 2, "role_id": two.id}])
        await self.reach(self.member, 1)
        self.assertIn(one, self.member.roles)
        await self.reach(self.member, 2)
        self.assertIn(one, self.member.roles)
        self.assertIn(two, self.member.roles)

    async def test_replace_keeps_only_the_highest(self):
        one, two = self.guild.role("One", 3), self.guild.role("Two", 4)
        await self.turn_on(replace_rewards=True, rewards=[{"level": 1, "role_id": one.id}, {"level": 2, "role_id": two.id}])
        await self.reach(self.member, 1)
        self.assertIn(one, self.member.roles)
        await self.reach(self.member, 2)
        self.assertNotIn(one, self.member.roles)
        self.assertIn(two, self.member.roles)

    async def test_replace_keeps_the_highest_reward_the_bot_can_actually_give(self):
        fine = self.guild.role("Fine", 3)
        high = self.guild.role("High", 20)
        await self.turn_on(replace_rewards=True, rewards=[{"level": 1, "role_id": fine.id}, {"level": 2, "role_id": high.id}])
        await self.reach(self.member, 1)
        self.assertIn(fine, self.member.roles)
        await self.reach(self.member, 2)
        self.assertIn(fine, self.member.roles)
        self.assertNotIn(high, self.member.roles)

    async def test_a_role_above_the_bot_or_with_power_is_skipped(self):
        high = self.guild.role("High", 20)
        strong = self.guild.role("Strong", 3, "ban_members")
        fine = self.guild.role("Fine", 2)
        await self.turn_on(rewards=[{"level": 1, "role_id": high.id}, {"level": 2, "role_id": strong.id}, {"level": 3, "role_id": fine.id}])
        await self.reach(self.member, 3)
        self.assertNotIn(high, self.member.roles)
        self.assertNotIn(strong, self.member.roles)
        self.assertIn(fine, self.member.roles)

    async def test_a_deleted_role_and_a_refused_change_do_not_raise(self):
        fine = self.guild.role("Fine", 2)
        await self.turn_on(rewards=[{"level": 1, "role_id": new_id()}, {"level": 2, "role_id": fine.id}])

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "no")
        self.member.add_roles = refuse
        await self.reach(self.member, 2)
        self.assertGreaterEqual(await self.xp_of(), xp_for_level(2))

    async def test_a_returning_member_gets_rewards_back_once_past_the_rules_screen(self):
        one = self.guild.role("One", 3)
        await self.turn_on(rewards=[{"level": 1, "role_id": one.id}])
        await self.cog_.set_xp(self.guild.id, self.member.id, 500)
        await self.cog_.on_member_remove(self.member)
        back = FakeMember(self.guild, "back", pending=True)
        back.id = self.member.id
        await self.cog_.on_member_join(back)
        self.assertNotIn(one, back.roles)
        passed = SimpleNamespace(pending=True)
        back.pending = False
        await self.cog_.on_member_update(passed, back)
        self.assertIn(one, back.roles)

    async def test_lowering_a_level_by_hand_takes_higher_rewards_away(self):
        one, two = self.guild.role("One", 3), self.guild.role("Two", 4)
        await self.turn_on(rewards=[{"level": 1, "role_id": one.id}, {"level": 5, "role_id": two.id}])
        ctx = self.context()
        await self.cog_.level_set_level.callback(self.cog_, ctx, 6, self.member)
        self.assertIn(two, self.member.roles)
        await self.cog_.level_set_level.callback(self.cog_, ctx, 2, self.member)
        self.assertNotIn(two, self.member.roles)
        self.assertIn(one, self.member.roles)


class VoiceTest(LevelsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        self.other = FakeMember(self.guild, "other", self.role_member)

    def join(self, *members, deaf: bool = False) -> None:
        for member in members:
            member.voice = SimpleNamespace(self_deaf=deaf, deaf=False)
            self.lounge.members.append(member)

    async def test_two_people_talking_both_earn(self):
        await self.turn_on(voice_xp=True, custom_xp=10)
        self.join(self.member, self.other)
        await self.cog_.voice_tick()
        self.assertEqual(await self.xp_of(), 10)
        self.assertEqual(await self.xp_of(self.other), 10)
        self.assertEqual(await self.db.scalar("SELECT voice_minutes FROM level_members WHERE user_id = ?", (self.member.id,)), 1)

    async def test_alone_or_with_only_a_bot_earns_nothing(self):
        await self.turn_on(voice_xp=True, custom_xp=10)
        robot = FakeMember(self.guild, "music", bot=True)
        self.join(self.member, robot)
        await self.cog_.voice_tick()
        self.assertEqual(await self.xp_of(), 0)

    async def test_deafened_people_and_the_afk_channel_earn_nothing(self):
        await self.turn_on(voice_xp=True, custom_xp=10)
        self.join(self.member, deaf=True)
        self.join(self.other)
        await self.cog_.voice_tick()
        self.assertEqual(await self.xp_of(), 0)
        self.assertEqual(await self.xp_of(self.other), 10)
        self.guild.afk_channel = self.lounge
        await self.cog_.voice_tick()
        self.assertEqual(await self.xp_of(self.other), 10)

    async def test_off_means_off(self):
        await self.turn_on(custom_xp=10)
        self.join(self.member, self.other)
        await self.cog_.voice_tick()
        self.assertEqual(await self.xp_of(), 0)

    async def test_the_timer_keeps_going_and_survives_a_failed_round(self):
        await self.turn_on(voice_xp=True)
        with mock.patch.object(self.cog_, "voice_tick", side_effect=RuntimeError("boom")):
            await self.bot.timers.run_due(time.time() + 86400 * 2)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertIsNotNone(await self.bot.timers.get_key("levels.voice", "levels.voice"))
        self.assertIsNotNone(await self.bot.timers.get_key("levels.backup", "levels.backup"))


class CommandTest(LevelsCase):
    async def test_settings_commands_save_and_log_who(self):
        ctx = self.context()
        await self.cog_.level_mod.callback(self.cog_, ctx, 5)
        await self.cog_.level_limit.callback(self.cog_, ctx, 10)
        await self.cog_.level_rewardonly.callback(self.cog_, ctx)
        await self.cog_.level_voice_xp.callback(self.cog_, ctx)
        await self.cog_.level_rate.callback(self.cog_, ctx, rate="2/30")
        await self.cog_.level_channel.callback(self.cog_, ctx, None)
        cfg = self.settings("levels")
        self.assertEqual((cfg["modulo"], cfg["minimum"], cfg["only_rewards"], cfg["voice_xp"], cfg["rate"], cfg["window"], cfg["destination"]), (5, 10, True, True, 2, 30, self.channel.id))
        rows = await self.bot.activity_log.recent(self.guild.id)
        self.assertEqual(rows[0]["user_name"], "admin")

    async def test_a_bad_rate_is_refused_with_a_sentence(self):
        ctx = self.context()
        await self.cog_.level_rate.callback(self.cog_, ctx, rate="soon")
        await self.cog_.level_rate.callback(self.cog_, ctx, rate="99/1")
        self.assertEqual(len(ctx.fails), 2)
        self.assertEqual(self.settings("levels")["rate"], 1)

    async def test_config_sets_card_colors_and_refuses_nonsense(self):
        ctx = self.context()
        await self.cog_.level_config.callback(self.cog_, ctx, "color", "#ff8800")
        await self.cog_.level_config.callback(self.cog_, ctx, "accent", "112233")
        await self.cog_.level_config.callback(self.cog_, ctx, "opacity", "40")
        cfg = self.settings("levels")
        self.assertEqual((cfg["card_color"], cfg["card_accent"], cfg["card_opacity"]), (0xFF8800, 0x112233, 40.0))
        for choice, value in (("color", "orange"), ("opacity", "heavy"), ("opacity", "nan"), ("opacity", None)):
            await self.cog_.level_config.callback(self.cog_, ctx, choice, value)
        self.assertEqual(len(ctx.fails), 4)
        await self.cog_.level_config.callback(self.cog_, ctx, None, None)
        self.assertIn("Level settings", ctx.sent[0]["embed"].title)

    async def test_message_set_and_reset(self):
        ctx = self.context()
        await self.cog_.level_message.callback(self.cog_, ctx, "set", message="Up to {lvl}!")
        self.assertEqual(self.settings("levels")["message"]["content"], "Up to {lvl}!")
        await self.cog_.level_message.callback(self.cog_, ctx, "set", message=None)
        self.assertEqual(len(ctx.fails), 1)
        await self.cog_.level_message.callback(self.cog_, ctx, "set", message="x" * 2001)
        self.assertEqual(len(ctx.fails), 2)
        await self.cog_.level_message.callback(self.cog_, ctx, "reset", message=None)
        self.assertEqual(self.settings("levels")["message"], DEFAULT_MESSAGE)

    async def test_toggles(self):
        ctx = self.context()
        await self.cog_.level_toggle.callback(self.cog_, ctx, "dm")
        self.assertEqual(self.settings("levels")["destination"], "dm")
        await self.cog_.level_toggle.callback(self.cog_, ctx, "dm")
        self.assertEqual(self.settings("levels")["destination"], "same")
        await self.cog_.level_toggle.callback(self.cog_, ctx, "announcements")
        self.assertEqual(self.settings("levels")["destination"], "none")
        await self.cog_.level_toggle.callback(self.cog_, ctx, "replace")
        self.assertTrue(self.settings("levels")["replace_rewards"])

    async def test_message_xp_command(self):
        ctx = self.context()
        await self.cog_.level_set_message_xp.callback(self.cog_, ctx, "set", 50)
        self.assertEqual(self.settings("levels")["custom_xp"], 50)
        await self.cog_.level_set_message_xp.callback(self.cog_, ctx, "set", None)
        self.assertEqual(len(ctx.fails), 1)
        await self.cog_.level_set_message_xp.callback(self.cog_, ctx, "default", None)
        self.assertIsNone(self.settings("levels")["custom_xp"])

    async def test_add_xp_and_set_level_work_on_someone_who_left(self):
        gone = SimpleNamespace(id=new_id(), bot=False, mention="<@1>", display_name="gone")
        await self.cog_.set_xp(self.guild.id, gone.id, 0, name="gone")
        ctx = self.context()
        await self.cog_.level_add_xp.callback(self.cog_, ctx, 500, gone)
        await self.cog_.level_add_xp.callback(self.cog_, ctx, 500, gone)
        self.assertEqual(await self.xp_of(gone), 1000)
        await self.cog_.level_set_level.callback(self.cog_, ctx, 3, gone)
        self.assertEqual(await self.xp_of(gone), xp_for_level(3))

    async def test_bots_are_refused(self):
        robot = FakeMember(self.guild, "robot", bot=True)
        ctx = self.context()
        await self.cog_.level_add_xp.callback(self.cog_, ctx, 500, robot)
        await self.cog_.level_set_level.callback(self.cog_, ctx, 5, robot)
        self.assertEqual(len(ctx.fails), 2)

    async def test_blacklist_add_show_remove(self):
        ctx = self.context()
        await self.cog_.level_blacklist_add.callback(self.cog_, ctx, targets=f"{self.channel.mention} {self.role_muted.mention}")
        cfg = self.settings("levels")
        self.assertEqual((cfg["blacklist_channels"], cfg["blacklist_roles"]), ([self.channel.id], [self.role_muted.id]))
        await self.cog_.level_blacklist.callback(self.cog_, ctx)
        self.assertIn(self.channel.mention, ctx.sent[0]["content"])
        await self.cog_.level_blacklist_add.callback(self.cog_, ctx, targets="nothing-called-this")
        self.assertEqual(len(ctx.fails), 1)
        self.assertIn("nothing-called-this", ctx.fails[0])
        await self.cog_.level_blacklist_remove.callback(self.cog_, ctx, targets="general Muted")
        cfg = self.settings("levels")
        self.assertEqual((cfg["blacklist_channels"], cfg["blacklist_roles"]), ([], []))

    async def test_reward_command_checks_the_role(self):
        ctx = self.context()
        await self.cog_.level_reward.callback(self.cog_, ctx, 5, self.role_member)
        self.assertEqual(self.settings("levels")["rewards"], [{"level": 5, "role_id": self.role_member.id}])
        high = self.guild.role("High", 20)
        strong = self.guild.role("Strong", 3, "ban_members")
        managed = self.guild.role("Managed", 3, managed=True)
        for role in (high, strong, managed, self.guild.default_role):
            await self.cog_.level_reward.callback(self.cog_, ctx, 6, role)
        self.assertEqual(len(ctx.fails), 4)
        self.assertEqual(len(self.settings("levels")["rewards"]), 1)

    async def test_a_moderator_cannot_reward_a_role_at_or_above_their_own(self):
        ctx = self.context(author=self.mod)
        await self.cog_.level_reward.callback(self.cog_, ctx, 5, self.role_mod)
        self.assertEqual(len(ctx.fails), 1)
        self.assertEqual(self.settings("levels")["rewards"], [])

    async def test_reward_with_no_role_removes_it(self):
        ctx = self.context()
        await self.cog_.level_reward.callback(self.cog_, ctx, 5, self.role_member)
        await self.cog_.level_reward.callback(self.cog_, ctx, 5, None)
        self.assertEqual(self.settings("levels")["rewards"], [])

    async def test_card_needs_levels_on_and_a_person(self):
        ctx = self.context()
        await self.cog_.level_member.callback(self.cog_, ctx, None)
        self.assertEqual(len(ctx.fails), 1)
        await self.turn_on()
        robot = FakeMember(self.guild, "robot", bot=True)
        await self.cog_.level_member.callback(self.cog_, ctx, robot)
        self.assertEqual(len(ctx.fails), 2)

    async def test_card_is_a_real_png_of_the_right_size(self):
        await self.turn_on()
        await self.cog_.set_xp(self.guild.id, self.member.id, 500)
        ctx = self.context(author=self.member)
        await self.cog_.level.callback(self.cog_, ctx)
        picture = ctx.sent[0]["file"]
        self.assertEqual(picture.filename, "rank.png")
        self.assertEqual(Image.open(picture.fp).size, (rankcard.WIDTH, rankcard.HEIGHT))

    async def test_card_uses_the_avatar_when_it_can_be_read(self):
        class Avatar:
            def replace(self, **kwargs):
                return self

            async def read(self):
                return png(128, 128, (0, 255, 0))

        await self.turn_on()
        await self.cog_.set_xp(self.guild.id, self.member.id, 500)
        self.member.display_avatar = Avatar()
        ctx = self.context(author=self.member)
        await self.cog_.level_member.callback(self.cog_, ctx, None)
        card = Image.open(ctx.sent[0]["file"].fp).convert("RGB")
        self.assertEqual(card.getpixel((160, 141)), (0, 255, 0))

    async def test_leaderboard_lists_by_xp_and_does_not_ping(self):
        await self.turn_on()
        await self.cog_.set_xp(self.guild.id, self.member.id, 500, name="member")
        await self.cog_.set_xp(self.guild.id, self.mod.id, 900, name="mod")
        ctx = self.context()
        await self.cog_.leaderboard.callback(self.cog_, ctx, 1)
        text_ = ctx.sent[0]["embed"].description
        self.assertLess(text_.index("mod"), text_.index("member"))
        self.assertNotIn("view", ctx.sent[0])
        mentions = ctx.sent[0]["allowed_mentions"]
        self.assertFalse(mentions.users or mentions.everyone or mentions.roles)

    async def test_leaderboard_escapes_names_and_pages(self):
        await self.turn_on()
        for index in range(25):
            await self.cog_.set_xp(self.guild.id, new_id(), 1000 - index, name=f"*user{index}*")
        ctx = self.context()
        await self.cog_.leaderboard.callback(self.cog_, ctx, 2)
        sent = ctx.sent[0]
        self.assertIn("\\*user10\\*", sent["embed"].description)
        self.assertNotIn("\\*user9\\*", sent["embed"].description)
        self.assertIsInstance(sent["view"], LeaderboardView)
        self.assertEqual(sent["embed"].footer.text, "Page 2 of 3, 25 members")

    async def test_page_buttons_are_for_the_person_who_asked(self):
        await self.turn_on()
        for index in range(25):
            await self.cog_.set_xp(self.guild.id, new_id(), 1000 - index, name=f"user{index}")
        view = LeaderboardView(self.cog_, self.guild.id, self.admin.id, 1, 3)
        self.assertTrue(view.previous_page.disabled)
        self.assertFalse(view.next_page.disabled)
        answers = []

        class Response:
            async def send_message(self, text_, ephemeral=False):
                answers.append(text_)

            async def edit_message(self, **kwargs):
                answers.append(kwargs)

        stranger = SimpleNamespace(user=self.member, response=Response())
        self.assertFalse(await view.interaction_check(stranger))
        self.assertIn("Only the person", answers[0])
        owner = SimpleNamespace(user=self.admin, response=Response())
        self.assertTrue(await view.interaction_check(owner))
        await view.next_page.callback(owner)
        self.assertIn("user10", answers[1]["embed"].description)
        self.assertEqual(view.page, 2)
        self.assertFalse(view.previous_page.disabled)

    async def test_people_who_left_drop_off_the_board(self):
        await self.turn_on()
        await self.cog_.set_xp(self.guild.id, self.member.id, 900, name="member")
        await self.cog_.set_xp(self.guild.id, self.mod.id, 500, name="mod")
        await self.cog_.on_member_remove(self.member)
        data = await self.cog_.board(self.guild.id)
        self.assertEqual([row["user_id"] for row in data["rows"]], [self.mod.id])
        self.assertEqual(data["rows"][0]["rank"], 1)
        data = await self.cog_.board(self.guild.id, include_left=True)
        self.assertEqual(len(data["rows"]), 2)
        await self.cog_.on_member_join(self.member)
        self.assertEqual(len((await self.cog_.board(self.guild.id))["rows"]), 2)

    async def test_startup_marks_those_who_left_while_the_bot_was_down(self):
        await self.turn_on()
        gone = new_id()
        await self.cog_.set_xp(self.guild.id, gone, 900, name="gone")
        await self.cog_.set_xp(self.guild.id, self.mod.id, 500, name="mod")
        await self.cog_.on_ready()
        data = await self.cog_.board(self.guild.id)
        self.assertEqual([row["user_id"] for row in data["rows"]], [self.mod.id])


class ResetAndBackupTest(LevelsCase):
    async def test_reset_one_member(self):
        await self.turn_on()
        await self.cog_.set_xp(self.guild.id, self.member.id, 900)
        await self.cog_.set_xp(self.guild.id, self.mod.id, 500)
        ctx = self.context()
        await self.cog_.level_reset.callback(self.cog_, ctx, "member", self.member)
        self.assertEqual(await self.xp_of(), 0)
        self.assertEqual(await self.xp_of(self.mod), 500)
        await self.cog_.level_reset.callback(self.cog_, ctx, "member", self.member)
        await self.cog_.level_reset.callback(self.cog_, ctx, "member", None)
        self.assertEqual(len(ctx.fails), 2)

    async def test_reset_server_keeps_a_backup_and_restore_brings_it_back(self):
        await self.turn_on(custom_xp=10)
        await self.cog_.set_xp(self.guild.id, self.member.id, 900, name="member")
        ctx = self.context()
        await self.cog_.level_reset.callback(self.cog_, ctx, "server", None)
        self.assertEqual(await self.xp_of(), 0)
        entry = await self.cog_._entry(self.guild.id, self.member.id)
        self.assertEqual(entry.xp, 0)
        await self.cog_.level_restore.callback(self.cog_, ctx)
        self.assertEqual(await self.xp_of(), 900)
        self.assertEqual((await self.cog_._entry(self.guild.id, self.member.id)).xp, 900)

    async def test_a_restored_server_backup_is_not_undone_by_old_xp_in_memory(self):
        await self.turn_on(custom_xp=10)
        await self.say()
        await self.cog_.flush()
        await self.db.run("UPDATE level_members SET xp = 5000 WHERE user_id = ?", (self.member.id,))  # what a restored backup does
        await self.cog_.on_guild_restored(self.guild.id)
        self.now += 61
        await self.say()
        self.assertEqual(await self.xp_of(), 5010)

    async def test_restore_with_no_backup_says_so(self):
        ctx = self.context()
        await self.cog_.level_restore.callback(self.cog_, ctx)
        self.assertEqual(len(ctx.fails), 1)

    async def test_a_restore_can_itself_be_undone(self):
        await self.cog_.set_xp(self.guild.id, self.member.id, 900)
        await self.cog_.reset_server(self.guild.id)
        await self.cog_.set_xp(self.guild.id, self.member.id, 50)
        await self.cog_.restore_backup(self.guild.id)
        self.assertEqual(await self.xp_of(), 900)
        await self.cog_.restore_backup(self.guild.id)
        self.assertEqual(await self.xp_of(), 50)

    async def test_only_the_newest_backups_stay(self):
        await self.cog_.set_xp(self.guild.id, self.member.id, 900)
        for _ in range(10):
            await self.cog_.snapshot(self.guild.id, "daily")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM level_backups WHERE guild_id = ?", (self.guild.id,)), 7)

    async def test_the_daily_backup_runs_from_its_timer(self):
        await self.turn_on()
        await self.cog_.set_xp(self.guild.id, self.member.id, 900)
        await self.bot.timers.run_due(time.time() + 86400 * 2)
        for task in list(self.cog_._tasks):
            await task
        rows = await self.db.all("SELECT reason FROM level_backups WHERE guild_id = ?", (self.guild.id,))
        self.assertEqual(rows, [{"reason": "daily"}])


class BackgroundTest(LevelsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.turn_on()
        self.web = FakeOutside()
        self.cog_.outside = self.web

    async def stored(self, owner) -> bytes | None:
        return await self.cog_._card_background(self.guild.id, owner)

    async def test_attachment_becomes_a_stored_card_sized_jpeg(self):
        ctx = self.context(author=self.member)
        await self.cog_.level_background.callback(self.cog_, ctx, None, FakeAttachment(png(1200, 900)))
        self.assertEqual(ctx.fails, [])
        picture = Image.open(io.BytesIO(await self.stored(self.member.id)))
        self.assertEqual((picture.format, picture.size), ("JPEG", (rankcard.WIDTH, rankcard.HEIGHT)))

    async def test_a_link_is_fetched_as_a_stranger_and_a_personal_one_beats_the_servers(self):
        self.web.answer("https://images.example/", reply(png(1000, 400, (0, 0, 255))))
        ctx = self.context()
        await self.cog_.level_serverbackground.callback(self.cog_, ctx, "https://images.example/server.png", None)
        self.assertTrue(self.web.calls[0]["stranger"])
        server_only = await self.stored(self.member.id)
        self.assertIsNotNone(server_only)
        self.web.answer("https://images.example/", reply(png(1000, 400, (0, 255, 0))))
        ctx = self.context(author=self.member)
        await self.cog_.level_background.callback(self.cog_, ctx, "https://images.example/mine.png", None)
        self.assertNotEqual(await self.stored(self.member.id), server_only)
        self.assertEqual(await self.stored(self.mod.id), server_only)
        await self.cog_.level_background.callback(self.cog_, ctx, None, None)
        self.assertEqual(await self.stored(self.member.id), server_only)

    async def test_refusals_explain_themselves_and_store_nothing(self):
        ctx = self.context(author=self.member)
        self.web.answer("https://images.example/404", reply(b"", 404))
        self.web.answer("https://images.example/text", reply(b"not a picture"))
        self.web.answer("https://images.example/tiny", reply(png(10, 10)))
        self.web.answer("https://images.example/wide", reply(png(5000, 100)))
        for tail in ("404", "text", "tiny", "wide", "down"):
            await self.cog_.level_background.callback(self.cog_, ctx, f"https://images.example/{tail}", None)
        await self.cog_.level_background.callback(self.cog_, ctx, None, FakeAttachment(b"x" * (9 * 1024 * 1024)))
        await self.cog_.level_background.callback(self.cog_, ctx, None, FakeAttachment(png(), fail=True))
        self.assertEqual(len(ctx.fails), 7)
        self.assertIsNone(await self.stored(self.member.id))

    async def test_a_corrupt_stored_background_falls_back(self):
        png_bytes = rankcard.draw(name="x", level=1, rank=1, into_level=1, level_size=2, background=b"broken", avatar=b"broken")
        self.assertEqual(Image.open(io.BytesIO(png_bytes)).size, (rankcard.WIDTH, rankcard.HEIGHT))


class OutsideLinkTest(BotTest):
    async def test_links_to_private_places_are_refused(self):
        for link in ("http://127.0.0.1/a.png", "http://[::1]/a.png", "http://10.0.0.8/a.png", "http://169.254.169.254/latest", "http://[::ffff:127.0.0.1]/a.png", "ftp://example.com/a", "file:///etc/passwd", "https://user:pw@example.com/a", "https://example.com:8443/a", "nonsense", ""):
            with self.assertRaises(OutsideError, msg=link):
                check_public_link(link)
        check_public_link("https://example.com/a.png")
        check_public_link("http://example.com:80/a.png")
        check_public_link("https://8.8.8.8/a.png")


class CardDrawingTest(BotTest):
    async def test_settings_change_the_picture(self):
        base = rankcard.draw(name="Zoe", level=3, rank=1, into_level=100, level_size=295)
        self.assertNotEqual(base, rankcard.draw(name="Zoe", level=3, rank=1, into_level=100, level_size=295, color=0xFF0000))
        self.assertNotEqual(base, rankcard.draw(name="Zoe", level=3, rank=1, into_level=100, level_size=295, accent=0xFF0000))
        self.assertNotEqual(base, rankcard.draw(name="Zoe", level=3, rank=1, into_level=100, level_size=295, opacity=10))

    async def test_a_long_or_undrawable_name_still_draws(self):
        for name in ("n" * 200, "漢字", "\U0001D401\U0001D428\U0001D425\U0001D41D", "", "   "):
            self.assertTrue(rankcard.draw(name=name, level=0, rank=None, into_level=0, level_size=100))

    async def test_names_lose_only_what_the_font_lacks(self):
        self.assertEqual(rankcard.drawable("Plain Name", "x"), "Plain Name")
        self.assertEqual(rankcard.drawable("Кирилл", "x"), "Кирилл")
        self.assertEqual(rankcard.drawable("漢字", "fallback"), "fallback")

    async def test_short_numbers(self):
        self.assertEqual([rankcard.short_number(n) for n in (999, 6000, 17975, 1990855)], ["999", "6.0k", "18.0k", "2.0M"])


class Mee6Test(LevelsCase):
    def page(self, players, role_rewards=None) -> dict:
        return {"page": 0, "players": players, "role_rewards": role_rewards or [], "xp_rate": 1.0, "xp_per_message": [15, 25]}

    def player(self, user_id, xp, name="someone") -> dict:
        return {"id": str(user_id), "username": name, "xp": xp, "level": level_for_xp(xp), "message_count": 12, "detailed_xp": [0, 0, xp]}

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.web = FakeOutside()
        self.cog_.outside = self.web
        self.cog_._sleep = mock.AsyncMock()
        self.bot.ops = DiscordOps(self.bot)
        self.url = MEE6_URL.format(guild_id=self.guild.id)

    async def test_import_replaces_xp_keeps_a_backup_and_takes_rewards(self):
        await self.cog_.set_xp(self.guild.id, self.mod.id, 5, name="mod")
        rewards = [
            {"rank": 5, "role": {"id": str(self.role_member.id)}},
            {"rank": 5, "role": {"id": str(self.role_muted.id)}},
            {"rank": 10, "role": {"id": str(new_id())}},
        ]
        self.web.answer(self.url, self.page([self.player(self.member.id, 5775), self.player(self.admin.id, 100)], rewards))
        ctx = self.context()
        await self.cog_.level_mee6import.callback(self.cog_, ctx)
        self.assertEqual(ctx.fails, [])
        self.assertEqual(await self.xp_of(), 5775)
        self.assertEqual(await self.xp_of(self.admin), 100)
        self.assertEqual(await self.xp_of(self.mod), 0)
        self.assertEqual(self.settings("levels")["rewards"], [{"level": 5, "role_id": self.role_member.id}])
        self.assertEqual(await self.db.scalar("SELECT reason FROM level_backups WHERE guild_id = ?", (self.guild.id,)), "before import")
        await self.cog_.level_restore.callback(self.cog_, ctx)
        self.assertEqual(await self.xp_of(self.mod), 5)

    async def test_it_walks_every_page(self):
        with mock.patch("captainguild.modules.levels.MEE6_PAGE", 2):
            def pages(call):
                page = call["params"]["page"]
                return self.page([self.player(new_id(), 100), self.player(new_id(), 200)] if page < 2 else [self.player(new_id(), 300)])
            self.web.answer(self.url, pages)
            job = await self.cog_.import_mee6(self.guild.id, None, self.cog_._start_import(self.guild.id))
        self.assertEqual(job["members"], 5)
        self.assertEqual(len(self.web.calls), 3)

    async def test_a_private_or_missing_leaderboard_changes_nothing(self):
        await self.cog_.set_xp(self.guild.id, self.member.id, 900)
        for status, phrase in ((401, "private"), (404, "no leaderboard"), (500, "500")):
            self.web.answer(self.url, reply({}, status))
            ctx = self.context()
            await self.cog_.level_mee6import.callback(self.cog_, ctx)
            self.assertIn(phrase, ctx.fails[0])
        self.assertEqual(await self.xp_of(), 900)

    async def test_it_waits_when_asked_to_slow_down(self):
        answers = [reply({}, 429, {"Retry-After": "7"}), reply(self.page([self.player(self.member.id, 100)]))]
        self.web.answer(self.url, lambda call: answers.pop(0))
        await self.cog_.import_mee6(self.guild.id, None, self.cog_._start_import(self.guild.id))
        self.cog_._sleep.assert_awaited_with(7.0)
        self.assertEqual(await self.xp_of(), 100)

    async def test_a_service_outage_is_a_sentence(self):
        ctx = self.context()
        await self.cog_.level_mee6import.callback(self.cog_, ctx)
        self.assertIn("MEE6", ctx.fails[0])

    async def test_two_imports_cannot_run_at_once(self):
        self.cog_._start_import(self.guild.id)
        with self.assertRaises(ValueError):
            self.cog_._start_import(self.guild.id)


class DashboardTest(LevelsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.web = FakeOutside()
        self.cog_.outside = self.web

    async def test_settings_save_and_bad_input(self):
        client = await self.client()
        response = await client.put(self.path("/settings/levels"), json={"enabled": True, "destination": str(self.channel.id), "rewards": [{"level": 3, "role_id": str(self.role_member.id)}]})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("levels")["destination"], self.channel.id)
        self.assertEqual(self.settings("levels")["rewards"], [{"level": 3, "role_id": self.role_member.id}])
        for form, field in (
            ({"destination": "somewhere"}, "destination"),
            ({"rewards": [{"level": 3, "role_id": "x"}]}, "rewards"),
            ({"rewards": [{"level": 3, "role_id": str(self.role_member.id)}, {"level": 3, "role_id": str(self.role_muted.id)}]}, "rewards"),
            ({"rate": 0}, "rate"),
            ({"window": 601}, "window"),
            ({"custom_xp": 101}, "custom_xp"),
            ({"card_opacity": -1}, "card_opacity"),
            ({"card_color": "orange"}, "card_color"),
            ({"message": {"content": "x" * 2001}}, "message"),
        ):
            response = await client.put(self.path("/settings/levels"), json=form)
            self.assertEqual(response.status, 400, form)
            self.assertIn(field, (await response.json())["fields"], form)

    async def test_leaderboard_endpoint_pages_and_searches(self):
        for index in range(30):
            await self.cog_.set_xp(self.guild.id, new_id(), 5000 - index, name=f"person{index}")
        await self.cog_.set_xp(self.guild.id, self.member.id, 9000, name="member")
        client = await self.client(access="staff")
        data = await (await client.get(self.path("/levels/leaderboard?size=10&page=1"))).json()
        self.assertEqual((data["total"], data["pages"], data["rows"][0]["rank"], data["rows"][0]["name"], data["rows"][0]["level"]), (31, 4, 1, "member", level_for_xp(9000)))
        self.assertEqual(data["rows"][0]["user_id"], str(self.member.id))
        self.assertEqual((await (await client.get(self.path("/levels/leaderboard?size=10&page=4"))).json())["rows"][-1]["rank"], 31)
        found = await (await client.get(self.path("/levels/leaderboard?q=person12"))).json()
        self.assertEqual([row["rank"] for row in found["rows"]], [14])
        found = await (await client.get(self.path(f"/levels/leaderboard?q={self.member.id}"))).json()
        self.assertEqual(found["rows"][0]["name"], "member")
        found = await (await client.get(self.path("/levels/leaderboard?q=%25"))).json()
        self.assertEqual(found["rows"], [])
        self.assertEqual((await client.get(self.path("/levels/leaderboard?page=abc"))).status, 200)

    async def test_another_servers_rows_never_show_or_change(self):
        other = FakeGuild("Other")
        self.bot.fake_guilds.append(other)
        await self.cog_.set_xp(other.id, self.member.id, 7777, name="elsewhere")
        await self.cog_.snapshot(other.id, "daily")
        await self.db.insert("INSERT INTO level_events (guild_id, user_id, level, at) VALUES (?, ?, ?, ?)", (other.id, self.member.id, 9, 1.0))
        foreign_backup = await self.db.scalar("SELECT id FROM level_backups WHERE guild_id = ?", (other.id,))
        client = await self.client()
        self.assertEqual((await (await client.get(self.path("/levels/leaderboard"))).json())["rows"], [])
        self.assertEqual(await (await client.get(self.path("/levels/events"))).json(), [])
        self.assertEqual(await (await client.get(self.path("/levels/backups"))).json(), [])
        self.assertEqual((await client.post(self.path(f"/levels/backups/{foreign_backup}/restore"))).status, 404)
        self.assertEqual((await client.delete(self.path(f"/levels/members/{self.member.id}"))).status, 404)
        await client.post(self.path("/levels/reset"))
        self.assertEqual((await self.cog_._entry(other.id, self.member.id)).xp, 7777)

    async def test_editing_a_member_records_it_and_syncs_roles_through_a_timer(self):
        one = self.guild.role("One", 3)
        await self.turn_on(rewards=[{"level": 2, "role_id": one.id}])
        client = await self.client()
        response = await client.put(self.path(f"/levels/members/{self.member.id}"), json={"level": 5})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["xp"], xp_for_level(5))
        await self.bot.timers.run_due(time.time() + 1)
        self.assertIn(one, self.member.roles)
        response = await client.put(self.path(f"/levels/members/{self.member.id}"), json={"xp": 10})
        self.assertEqual((await response.json())["level"], 0)
        await self.bot.timers.run_due(time.time() + 1)
        self.assertNotIn(one, self.member.roles)
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "level.member.update")

    async def test_editing_refuses_bad_input_and_strangers(self):
        client = await self.client()
        where = self.path(f"/levels/members/{self.member.id}")
        self.assertEqual((await client.put(where, json={})).status, 400)
        self.assertEqual((await client.put(where, json={"xp": 1, "level": 1})).status, 400)
        self.assertEqual((await client.put(where, json={"xp": -1})).status, 400)
        self.assertEqual((await client.put(where, json={"level": 5000})).status, 400)
        self.assertEqual((await client.put(where, json={"level": "x"})).status, 400)
        self.assertEqual((await client.put(self.path(f"/levels/members/{new_id()}"), json={"xp": 5})).status, 404)

    async def test_staff_can_look_but_not_change(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/levels/leaderboard"))).status, 200)
        self.assertEqual((await client.get(self.path("/levels/events"))).status, 200)
        self.assertEqual((await client.get(self.path("/levels/card"))).status, 200)
        self.assertEqual((await client.put(self.path(f"/levels/members/{self.member.id}"), json={"xp": 5})).status, 403)
        self.assertEqual((await client.delete(self.path(f"/levels/members/{self.member.id}"))).status, 403)
        self.assertEqual((await client.post(self.path("/levels/reset"))).status, 403)
        self.assertEqual((await client.post(self.path("/levels/import"))).status, 403)
        self.assertEqual((await client.get(self.path("/levels/backups"))).status, 403)
        self.assertEqual((await client.delete(self.path("/levels/background"))).status, 403)

    async def test_reset_and_restore_endpoints(self):
        await self.cog_.set_xp(self.guild.id, self.member.id, 900, name="member")
        client = await self.client()
        self.assertEqual((await (await client.post(self.path("/levels/reset"))).json())["members"], 1)
        rows = await (await client.get(self.path("/levels/backups"))).json()
        self.assertEqual((rows[0]["reason"], rows[0]["members"]), ("before reset", 1))
        self.assertNotIn("data", rows[0])
        self.assertEqual((await client.post(self.path(f"/levels/backups/{rows[0]['id']}/restore"))).status, 200)
        self.assertEqual(await self.xp_of(), 900)
        self.assertEqual((await self.bot.activity_log.recent(self.guild.id))[0]["action"], "level.restore")

    async def test_import_runs_in_the_background_and_reports(self):
        self.web.answer(MEE6_URL.format(guild_id=self.guild.id), {"players": [{"id": str(self.member.id), "xp": 500, "username": "member", "message_count": 3}], "role_rewards": []})
        client = await self.client()
        self.assertEqual((await (await client.get(self.path("/levels/import"))).json())["state"], "idle")
        response = await client.post(self.path("/levels/import"))
        self.assertEqual((await response.json())["state"], "running")
        for task in list(self.cog_._tasks):
            await task
        status = await (await client.get(self.path("/levels/import"))).json()
        self.assertEqual((status["state"], status["members"]), ("done", 1))
        self.assertEqual(await self.xp_of(), 500)

    async def test_a_failed_import_reports_why(self):
        self.web.answer(MEE6_URL.format(guild_id=self.guild.id), reply({}, 401))
        client = await self.client()
        await client.post(self.path("/levels/import"))
        for task in list(self.cog_._tasks):
            await task
        status = await (await client.get(self.path("/levels/import"))).json()
        self.assertEqual(status["state"], "failed")
        self.assertIn("private", status["error"])

    async def test_background_upload_preview_and_removal(self):
        client = await self.client()
        form = aiohttp.FormData()
        form.add_field("background", png(1000, 500), filename="b.png", content_type="image/png")
        response = await client.post(self.path("/levels/background"), data=form)
        self.assertEqual(response.status, 200)
        self.assertTrue((await (await client.get(self.path("/levels/background"))).json())["set"])
        response = await client.get(self.path("/levels/card?color=%23ff0000&opacity=30"))
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(Image.open(io.BytesIO(await response.read())).size, (rankcard.WIDTH, rankcard.HEIGHT))
        self.assertEqual((await client.delete(self.path("/levels/background"))).status, 200)
        self.assertFalse((await (await client.get(self.path("/levels/background"))).json())["set"])

    async def test_uploads_are_checked(self):
        client = await self.client()
        form = aiohttp.FormData()
        form.add_field("background", b"not an image", filename="b.png", content_type="image/png")
        self.assertEqual((await client.post(self.path("/levels/background"), data=form)).status, 400)
        form = aiohttp.FormData()
        form.add_field("other", png(), filename="b.png", content_type="image/png")
        self.assertEqual((await client.post(self.path("/levels/background"), data=form)).status, 400)
        self.assertEqual((await client.post(self.path("/levels/background"), json={"background": 1})).status, 400)
        form = aiohttp.FormData()
        form.add_field("background", b"x" * (9 * 1024 * 1024), filename="b.png", content_type="image/png")
        self.assertEqual((await client.post(self.path("/levels/background"), data=form)).status, 400)

    async def test_card_preview_refuses_bad_colors(self):
        client = await self.client()
        self.assertEqual((await client.get(self.path("/levels/card?color=orange"))).status, 400)
        self.assertEqual((await client.get(self.path("/levels/card?opacity=heavy"))).status, 400)
        self.assertEqual((await client.get(self.path("/levels/card?opacity=nan"))).status, 400)

    async def test_stored_settings_round_trip_through_json_for_backups(self):
        await self.turn_on()
        await self.cog_._store_background(self.guild.id, 0, png())
        rows = await self.db.all("SELECT * FROM level_backgrounds WHERE guild_id = ?", (self.guild.id,))
        self.assertTrue(json.dumps(rows))
