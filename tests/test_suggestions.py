from types import SimpleNamespace

import discord

from captainguild.core.module import required_permissions
from captainguild.core.ops import DiscordOps
from captainguild.modules.suggestions import DOWN, GREEN, NEUTRAL, RED, UP, Suggestions, vote_color
from tests.fakes import BotTest, FakeChannel, FakeGuild, FakeMember, _FakeResponse


class SuggestionsCase(BotTest):
    modules = [Suggestions]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.cog_ = self.cog(Suggestions)
        self.board = FakeChannel(self.guild, "suggestions")
        self.log_channel = FakeChannel(self.guild, "decisions")
        await self.configure(channel_id=self.board.id)
        self.actor = self.bot.actor(self.admin)

    async def configure(self, **form):
        await self.bot.settings.save(self.guild.id, "suggestions", form)

    async def suggest(self, content="Add a bird channel", member=None):
        ctx = self.context(author=member or self.member)
        await self.cog_.suggestion_suggest.callback(self.cog_, ctx, content=content)
        return ctx, self.board.sent[-1] if self.board.sent else None

    async def decide(self, number=1, decision="approved", reason="Good idea"):
        return await self.cog_.decide(self.guild.id, number, decision, moderator=self.actor, reason=reason)

    def votes(self, message, up=0, down=0):
        message.reactions = [SimpleNamespace(emoji=UP, count=up + 1, me=True), SimpleNamespace(emoji=DOWN, count=down + 1, me=True)]


class SubmittingTest(SuggestionsCase):
    async def test_a_suggestion_is_posted_numbered_and_gets_vote_reactions(self):
        ctx, post = await self.suggest()
        embed = post.embeds[0]
        self.assertEqual(embed.title, "Suggestion #1")
        self.assertEqual(embed.description, "Add a bird channel")
        self.assertEqual(embed.author.name, "member")
        self.assertEqual(post.added_reactions, [UP, DOWN])
        self.assertIn("#1", ctx.oks[0])
        self.assertFalse(post.kwargs["allowed_mentions"].users)
        _, second = await self.suggest("Another")
        self.assertEqual(second.embeds[0].title, "Suggestion #2")

    async def test_numbers_are_per_server(self):
        await self.suggest()
        await self.db.run("UPDATE suggestions SET guild_id = 999")
        _, post = await self.suggest()
        self.assertEqual(post.embeds[0].title, "Suggestion #1")

    async def test_anonymous_hides_the_name_but_who_still_knows(self):
        await self.configure(anon=True)
        _, post = await self.suggest()
        self.assertIsNone(post.embeds[0].author.name)
        ctx = self.context()
        await self.cog_.suggestion_who.callback(self.cog_, ctx, 1)
        self.assertIn(self.member.mention, ctx.sent[-1]["content"])
        self.assertFalse(ctx.sent[-1]["allowed_mentions"].users)

    async def test_turning_anonymous_on_doesnt_change_earlier_ones(self):
        await self.suggest()
        await self.configure(anon=True, edit=True)
        await self.decide()
        self.assertEqual(self.board.sent[0].embeds[0].author.name, "member")

    async def test_no_channel_refuses(self):
        await self.configure(channel_id=None)
        ctx, _ = await self.suggest()
        self.assertIn("no suggestion channel", ctx.fails[0])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM suggestions"), 0)

    async def test_another_servers_channel_in_the_setting_gets_nothing(self):
        other = FakeGuild("Elsewhere")
        self.bot.fake_guilds.append(other)
        elsewhere = FakeChannel(other, "elsewhere")
        await self.configure(channel_id=elsewhere.id)
        ctx, _ = await self.suggest()
        self.assertTrue(ctx.fails)
        self.assertEqual(elsewhere.sent, [])

    async def test_restricting_to_one_channel(self):
        place = FakeChannel(self.guild, "ideas")
        await self.configure(submit_channel=place.id)
        ctx, _ = await self.suggest()
        self.assertIn(place.mention, ctx.fails[0])
        ctx = self.context(author=self.member, channel=place)
        await self.cog_.suggestion_suggest.callback(self.cog_, ctx, content="Here")
        self.assertTrue(ctx.oks)

    async def test_length_and_blank_are_checked(self):
        ctx, _ = await self.suggest("x" * 4001)
        self.assertIn("4000", ctx.fails[0])
        ctx, _ = await self.suggest("   ")
        self.assertTrue(ctx.fails)

    async def test_a_refused_post_gives_the_number_back(self):
        real = self.board.send

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Missing Access")

        self.board.send = refuse
        ctx, _ = await self.suggest()
        self.assertTrue(ctx.fails)
        self.board.send = real
        _, post = await self.suggest()
        self.assertEqual(post.embeds[0].title, "Suggestion #1")

    async def test_votes_are_counted_from_reactions_and_bots_dont_count(self):
        _, post = await self.suggest()

        def event(user, emoji=UP, member=None):
            return SimpleNamespace(guild_id=self.guild.id, channel_id=self.board.id, message_id=post.id, user_id=user.id, emoji=discord.PartialEmoji(name=emoji), member=member or user)

        await self.cog_.on_raw_reaction_add(event(self.admin))
        await self.cog_.on_raw_reaction_add(event(self.mod, DOWN))
        await self.cog_.on_raw_reaction_add(event(self.me))
        await self.cog_.on_raw_reaction_add(event(FakeMember(self.guild, "robot", bot=True)))
        await self.cog_.on_raw_reaction_add(event(self.admin, "\N{SMILING FACE WITH SUNGLASSES}"))
        row = await self.db.one("SELECT up, down FROM suggestions")
        self.assertEqual(row, {"up": 1, "down": 1})
        await self.cog_.on_raw_reaction_remove(event(self.admin))
        self.assertEqual((await self.db.one("SELECT up FROM suggestions"))["up"], 0)
        await self.cog_.on_raw_reaction_remove(event(self.admin))
        self.assertEqual((await self.db.one("SELECT up FROM suggestions"))["up"], 0)
        await self.cog_.on_raw_reaction_clear(SimpleNamespace(guild_id=self.guild.id, channel_id=self.board.id, message_id=post.id))
        self.assertEqual((await self.db.one("SELECT down FROM suggestions"))["down"], 0)


class VoteColorTest(BotTest):
    async def test_colors(self):
        self.assertEqual(vote_color(0, 0, 5), NEUTRAL)
        self.assertEqual(vote_color(3, 3, 5), NEUTRAL)
        self.assertEqual(vote_color(5, 0, 5), GREEN)
        self.assertEqual(vote_color(9, 0, 5), GREEN)
        self.assertEqual(vote_color(0, 5, 5), RED)
        self.assertEqual(vote_color(1, 0, 0), GREEN)
        self.assertEqual(vote_color(0, 1, 0), RED)
        halfway = vote_color(5, 0, 10)
        self.assertNotIn(halfway, (NEUTRAL, GREEN))
        self.assertGreater(halfway & 0xFF00, NEUTRAL & 0xFF00)


class DecidingTest(SuggestionsCase):
    async def test_a_decision_moves_it_out_of_the_voting_channel_by_default(self):
        _, post = await self.suggest()
        row = await self.decide()
        self.assertTrue(post.deleted)
        entry = self.board.sent[-1]
        self.assertEqual(entry.embeds[0].title, "Suggestion #1: Approved")
        self.assertEqual(entry.embeds[0].fields[0].name, "Approved by admin")
        self.assertEqual(entry.embeds[0].fields[0].value, "Good idea")
        self.assertEqual((row["state"], row["decided_by_name"], row["entry_message_id"], row["message_id"]), ("approved", "admin", entry.id, None))

    async def test_a_decision_channel_takes_it(self):
        await self.configure(decision_channel=self.log_channel.id)
        _, post = await self.suggest()
        await self.decide()
        self.assertTrue(post.deleted)
        self.assertEqual(self.board.sent, [])
        self.assertEqual(self.log_channel.sent[-1].embeds[0].title, "Suggestion #1: Approved")

    async def test_each_decision_can_have_its_own_channel(self):
        denied = FakeChannel(self.guild, "denied")
        await self.configure(decision_channel=self.log_channel.id, deny_channel=denied.id)
        await self.suggest()
        await self.decide(decision="denied")
        self.assertEqual(denied.sent[-1].embeds[0].title, "Suggestion #1: Denied")
        self.assertEqual(self.log_channel.sent, [])
        await self.suggest()
        await self.decide(number=2)
        self.assertEqual(self.log_channel.sent[-1].embeds[0].title, "Suggestion #2: Approved")

    async def test_editing_changes_the_original_in_place(self):
        await self.configure(edit=True)
        _, post = await self.suggest()
        row = await self.decide()
        self.assertFalse(post.deleted)
        self.assertEqual(post.embeds[0].title, "Suggestion #1: Approved")
        self.assertEqual(len(self.board.sent), 1)
        self.assertIsNone(row["entry_message_id"])
        self.assertEqual(row["message_id"], post.id)

    async def test_editing_with_a_decision_channel_does_both(self):
        await self.configure(edit=True, decision_channel=self.log_channel.id)
        _, post = await self.suggest()
        await self.decide()
        self.assertEqual(post.embeds[0].title, "Suggestion #1: Approved")
        self.assertEqual(self.log_channel.sent[-1].embeds[0].title, "Suggestion #1: Approved")

    async def test_editing_a_post_someone_deleted_posts_it_new_instead(self):
        await self.configure(edit=True)
        _, post = await self.suggest()
        self.board.sent.remove(post)
        real = self.board.get_partial_message

        def gone(message_id):
            if message_id == post.id:
                async def missing(**kwargs):
                    raise discord.NotFound(_FakeResponse(404), "Unknown Message")
                return SimpleNamespace(edit=missing)
            return real(message_id)

        self.board.get_partial_message = gone
        row = await self.decide()
        self.assertEqual(self.board.sent[-1].embeds[0].title, "Suggestion #1: Approved")
        self.assertIsNone(row["message_id"])
        self.assertIsNotNone(row["entry_message_id"])

    async def test_considering_keeps_the_vote_going(self):
        _, post = await self.suggest()
        row = await self.decide(decision="considered", reason=None)
        self.assertFalse(post.deleted)
        entry = self.board.sent[-1]
        self.assertEqual(entry.embeds[0].title, "Suggestion #1: Being considered")
        self.assertEqual(entry.embeds[0].fields[0].value, "No reason given.")
        self.assertEqual(row["message_id"], post.id)
        row = await self.decide()
        self.assertTrue(post.deleted)
        self.assertTrue(entry.deleted)
        self.assertEqual(row["state"], "approved")
        self.assertEqual(len(self.board.sent), 1)

    async def test_the_color_and_count_come_from_the_real_reactions(self):
        await self.configure(edit=True, threshold=2)
        _, post = await self.suggest()
        self.votes(post, up=4, down=2)
        row = await self.decide()
        self.assertEqual((row["up"], row["down"]), (4, 2))
        self.assertEqual(post.embeds[0].color.value, GREEN)
        self.assertIn(f"{UP} 4", post.embeds[0].fields[1].value)
        _, post = await self.suggest()
        self.votes(post, up=0, down=2)
        await self.decide(number=2, decision="denied")
        self.assertEqual(post.embeds[0].color.value, RED)

    async def test_the_author_hears_when_that_is_on(self):
        await self.suggest()
        await self.decide()
        self.assertEqual(self.member.dms, [])
        await self.suggest()
        await self.configure(dm=True)
        await self.decide(number=2)
        embed = self.member.dms[-1]["embeds"][0]
        self.assertEqual(embed.description, "Add a bird channel")
        self.assertEqual(embed.fields[0].value, "Good idea")

    async def test_closed_dms_dont_stop_the_decision(self):
        await self.configure(dm=True)
        await self.suggest()

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(_FakeResponse(403), "Cannot send messages to this user")

        self.member.send = refuse
        row = await self.decide()
        self.assertEqual(row["state"], "approved")

    async def test_a_decision_channel_that_is_gone_falls_back(self):
        await self.configure(decision_channel=123456789012345678)
        await self.suggest()
        await self.decide()
        self.assertEqual(self.board.sent[-1].embeds[0].title, "Suggestion #1: Approved")

    async def test_commands_decide_and_answer(self):
        await self.suggest()
        ctx = self.context()
        await self.cog_.suggestion_approve.callback(self.cog_, ctx, 1, reason="Yes")
        self.assertIn("approved", ctx.oks[-1])
        await self.suggest()
        await self.cog_.suggestion_deny.callback(self.cog_, ctx, 2)
        await self.suggest()
        await self.cog_.suggestion_consider.callback(self.cog_, ctx, 3)
        await self.suggest()
        await self.cog_.suggestion_implemented.callback(self.cog_, ctx, 4, reason="Done")
        rows = await self.db.all("SELECT state FROM suggestions ORDER BY number")
        self.assertEqual([r["state"] for r in rows], ["approved", "denied", "considered", "implemented"])
        await self.cog_.suggestion_approve.callback(self.cog_, ctx, 99)
        self.assertIn("#99", ctx.fails[0])
        await self.cog_.suggestion_approve.callback(self.cog_, ctx, 1, reason="x" * 1001)
        self.assertIn("1000", ctx.fails[1])
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertIn("suggestion.denied", actions)


class SettingCommandTest(SuggestionsCase):
    async def test_toggles_channels_and_limits(self):
        ctx = self.context()
        for command, field in ((self.cog_.suggestion_dm, "dm"), (self.cog_.suggestion_edit, "edit"), (self.cog_.suggestion_anonymous, "anon")):
            before = self.settings("suggestions")[field]
            await command.callback(self.cog_, ctx)
            self.assertEqual(self.settings("suggestions")[field], not before)
        await self.cog_.suggestion_limit.callback(self.cog_, ctx, 10)
        self.assertEqual(self.settings("suggestions")["threshold"], 10)
        await self.cog_.suggestion_limit.callback(self.cog_, ctx, 101)
        self.assertTrue(ctx.fails)
        await self.cog_.suggestion_move.callback(self.cog_, ctx, self.log_channel)
        self.assertEqual(self.settings("suggestions")["decision_channel"], self.log_channel.id)
        await self.cog_.suggestion_move.callback(self.cog_, ctx)
        made = next(c for c in self.guild.channels if c.name == "suggestion-log")
        self.assertEqual(self.settings("suggestions")["decision_channel"], made.id)
        await self.cog_.suggestion_move.callback(self.cog_, ctx)
        self.assertEqual(len([c for c in self.guild.channels if c.name == "suggestion-log"]), 1)
        await self.cog_.suggestion_submit.callback(self.cog_, ctx, self.channel)
        self.assertEqual(self.settings("suggestions")["submit_channel"], self.channel.id)
        await self.cog_.suggestion_submit.callback(self.cog_, ctx)
        self.assertIsNone(self.settings("suggestions")["submit_channel"])
        await self.cog_.suggestion.callback(self.cog_, ctx)
        self.assertIn(self.board.mention, ctx.sent[-1]["content"])

    async def test_channel_uses_one_or_finds_one_by_name_or_makes_one(self):
        ctx = self.context()
        await self.cog_.suggestion_channel.callback(self.cog_, ctx, self.channel)
        self.assertEqual(self.settings("suggestions")["channel_id"], self.channel.id)
        await self.cog_.suggestion_channel.callback(self.cog_, ctx)
        self.assertEqual(self.settings("suggestions")["channel_id"], self.board.id)
        await self.cog_.suggestion_channel.callback(self.cog_, ctx, name="new:ideas")
        made = next(c for c in self.guild.channels if c.name == "ideas")
        self.assertEqual(self.settings("suggestions")["channel_id"], made.id)
        self.assertIs(made.overwrites[self.guild.default_role].send_messages, False)

    async def test_who_refuses_an_unknown_number(self):
        ctx = self.context()
        await self.cog_.suggestion_who.callback(self.cog_, ctx, 5)
        await self.cog_.suggestion_who.callback(self.cog_, ctx, 10 ** 30)
        await self.cog_.suggestion_approve.callback(self.cog_, ctx, 10 ** 30)
        self.assertEqual(len(ctx.fails), 3)

    async def test_deleting_a_channel_clears_it_from_the_settings(self):
        await self.configure(decision_channel=self.log_channel.id, approve_channel=self.log_channel.id)
        await self.cog_.on_guild_channel_delete(self.log_channel)
        cfg = self.settings("suggestions")
        self.assertIsNone(cfg["decision_channel"])
        self.assertIsNone(cfg["approve_channel"])
        self.assertEqual(cfg["channel_id"], self.board.id)

    async def test_a_deleted_post_is_noticed(self):
        _, post = await self.suggest()
        await self.cog_.on_raw_message_delete(SimpleNamespace(guild_id=self.guild.id, channel_id=self.board.id, message_id=post.id))
        self.assertIsNone(await self.db.scalar("SELECT message_id FROM suggestions"))

    async def test_who_may_use_what(self):
        for command in (self.cog_.suggestion, self.cog_.suggestion_channel, self.cog_.suggestion_dm, self.cog_.suggestion_limit, self.cog_.suggestion_move, self.cog_.suggestion_edit,
                        self.cog_.suggestion_submit, self.cog_.suggestion_anonymous, self.cog_.suggestion_who, self.cog_.suggestion_approve, self.cog_.suggestion_deny,
                        self.cog_.suggestion_consider, self.cog_.suggestion_implemented):
            self.assertEqual(required_permissions(command), ("manage_guild",), command.name)
        self.assertEqual(required_permissions(self.cog_.suggestion_suggest), ())


class EndpointTest(SuggestionsCase):
    async def test_the_list_filters_and_pages(self):
        for number in range(3):
            await self.suggest(f"Idea {number}")
        await self.decide(number=1)
        client = await self.client()
        data = await (await client.get(self.path("/suggestions/list"))).json()
        self.assertEqual((data["total"], data["rows"][0]["number"]), (3, 3))
        data = await (await client.get(self.path("/suggestions/list?state=approved"))).json()
        self.assertEqual([r["number"] for r in data["rows"]], [1])
        self.assertIsNotNone(data["rows"][0]["entry_url"])
        data = await (await client.get(self.path("/suggestions/list?q=idea%202"))).json()
        self.assertEqual([r["number"] for r in data["rows"]], [3])
        data = await (await client.get(self.path("/suggestions/list?q=member&page=2&size=2"))).json()
        self.assertEqual((data["total"], len(data["rows"]), data["page"]), (3, 1, 2))
        data = await (await client.get(self.path("/suggestions/list?page=x&size=y"))).json()
        self.assertEqual(data["page"], 1)
        response = await client.get(self.path("/suggestions/list?page=99999999999999999999999&size=100"))
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["rows"], [])

    async def test_decide_and_delete(self):
        _, post = await self.suggest()
        client = await self.client()
        response = await client.post(self.path("/suggestions/1/decide"), json={"decision": "denied", "reason": "No"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["state"], "denied")
        self.assertEqual((await client.post(self.path("/suggestions/1/decide"), json={"decision": "maybe"})).status, 400)
        self.assertEqual((await client.post(self.path("/suggestions/1/decide"), json={"decision": "denied", "reason": "x" * 1001})).status, 400)
        self.assertEqual((await client.post(self.path("/suggestions/9/decide"), json={"decision": "denied"})).status, 404)
        entry = self.board.sent[-1]
        self.assertEqual((await client.delete(self.path("/suggestions/1"))).status, 200)
        self.assertTrue(entry.deleted)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM suggestions"), 0)
        actions = [row["action"] for row in await self.bot.activity_log.recent(self.guild.id)]
        self.assertEqual(actions[:2], ["suggestion.delete", "suggestion.denied"])

    async def test_another_servers_suggestions_are_out_of_reach(self):
        await self.suggest()
        await self.db.run("UPDATE suggestions SET guild_id = 999")
        client = await self.client()
        self.assertEqual((await (await client.get(self.path("/suggestions/list"))).json())["total"], 0)
        self.assertEqual((await client.post(self.path("/suggestions/1/decide"), json={"decision": "denied"})).status, 404)
        self.assertEqual((await client.delete(self.path("/suggestions/1"))).status, 404)

    async def test_create_channel(self):
        client = await self.client()
        response = await client.post(self.path("/suggestions/create-channel"), json={"name": "ideas"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("suggestions")["channel_id"], next(c.id for c in self.guild.channels if c.name == "ideas"))
        self.assertEqual((await client.post(self.path("/suggestions/create-channel"), json={"name": ""})).status, 400)

    async def test_settings_save_and_check(self):
        client = await self.client()
        response = await client.put(self.path("/settings/suggestions"), json={"threshold": "5", "dm": True})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.settings("suggestions")["threshold"], 5)
        self.assertEqual((await client.put(self.path("/settings/suggestions"), json={"threshold": 101})).status, 400)
        self.assertEqual((await client.put(self.path("/settings/suggestions"), json={"approve_channel": "words"})).status, 400)

    async def test_staff_are_refused(self):
        client = await self.client(access="staff")
        self.assertEqual((await client.get(self.path("/suggestions/list"))).status, 403)
        self.assertEqual((await client.post(self.path("/suggestions/1/decide"), json={"decision": "denied"})).status, 403)
