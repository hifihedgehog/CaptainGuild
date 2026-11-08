"""Wiki answers in a server, through the real discord.py library with only the network replaced: the four AI commands, each person's limits
and the refunds, the word screen, automatic answers to new forum posts, the two admin commands, several sources, and the updates."""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core import access
from captainguild.core.answers import modelmaker, prompts, screening, servers, wiki
from captainguild.core.ops import DiscordOps
from captainguild.modules import answers as pack
from captainguild.modules.answers import Answers
from captainguild.modules.automod import Automod
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.answer_fakes import ScriptedModel, page_html, sitemap, write_term_lists
from tests.fake_outside import FakeOutside
from tests.fakes import BotTest, FakeMember
from tests.gateway import GatewayTest, iso, new_id
from tests.test_answers_engine import SVG
from tests.test_answers_modelmaker import OpenScad

BASE = "https://wiki.example.org"
SITEMAP = BASE + "/sitemap.xml"
DOCS = "https://docs.example.org"
NOZZLE = page_html("Replacing the Nozzle", '<h2 id="cool">Cool down</h2><p>Let the hotend cool before you touch the nozzle.</p><h2 id="swap">Swap</h2><p>Unscrew the nozzle with the wrench.</p>')
PLATE = page_html("Cleaning the Plate", "<p>Wash the plate with dish soap and warm water.</p>")
QUESTION = "How do I change the nozzle on my printer?"
WIKI_ROW = {"name": "the Sample Wiki", "kind": "sitemap", "address": SITEMAP, "prefix": "/en/", "include": [], "exclude": [], "selector": "", "refresh_hours": 24}
DOCS_ROW = {"name": "the Sample Docs", "kind": "crawl", "address": DOCS + "/guide/", "prefix": "/guide/", "include": [], "exclude": [], "selector": "", "refresh_hours": 12}
WORDS = {**prompts.SUBJECT, "wiki": "the Sample Wiki"}


def footer(command: str, sources: str = "the Sample Wiki") -> str:
    return prompts.fill(prompts.FOOTERS[command], {"sources": sources})


class AnswersCase(GatewayTest):
    modules = [Automod, Answers]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        for name, value in (("FETCH_PAUSE", 0), ("RETRY_WAIT", 0)):
            patcher = mock.patch.object(wiki, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        key = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
        key.start()
        self.addCleanup(key.stop)
        self.addCleanup(screening.use, None)
        servers.use({self.guild_id})  # the host lets this server use its key
        self.addCleanup(servers.use, set())
        # Members see and post in the server's channels, as in any server where they ask questions.
        rights = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True, read_message_history=True, use_application_commands=True)
        self.bot._connection.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**self.role_everyone, "permissions": str(rights.value)}})
        self.answers = self.bot.module("answers")
        self.model = ScriptedModel()
        self.answers.client = self.model.client()
        self.web = FakeOutside()
        self.web.answer(SITEMAP, sitemap(BASE, {"/en/nozzle": "2026-01-01", "/en/plate": "2026-01-01"}))
        self.web.answer(BASE + "/en/nozzle", NOZZLE)
        self.web.answer(BASE + "/en/plate", PLATE)
        self.web.answer(DOCS + "/robots.txt", "", status=404)
        self.web.answer(DOCS + "/guide", page_html("Guide", '<p>Start with <a href="wear">nozzle wear</a>.</p>'))
        self.web.answer(DOCS + "/guide/wear", page_html("Nozzle wear", "<p>A worn brass nozzle under-extrudes after abrasive filament.</p>"))
        self.answers.outside = self.web
        await self.configure(enabled=True, sources=[WIKI_ROW])
        await self.bot.sync_server_commands(self.guild_id)

    async def configure(self, **form) -> None:
        await self.bot.settings.save(self.guild_id, "answers", form)
        await self.drain()

    async def drain(self) -> None:
        """Wait for the pack's own background work, which the library's settle doesn't see."""
        for _ in range(100):
            await self.settle()
            waiting = [task for task in self.answers._tasks if not task.done()]
            if not waiting:
                return
            await asyncio.wait(waiting, timeout=10)

    def settings(self) -> dict:
        return self.bot.settings.get(self.guild_id, "answers")

    def source(self, number: int = 0) -> wiki.Source:
        return wiki.Source.of(self.settings()["sources"][number])

    async def slash(self, name: str, *, user_id: int | None = None, resolved: dict | None = None, **options) -> None:
        kinds = {bool: 5, int: 4}
        data = {"id": str(new_id()), "name": name, "type": 1, "guild_id": str(self.guild_id), "resolved": resolved or {},
                "options": [{"name": key, "type": 7 if key == "forum" else kinds.get(type(value), 3), "value": value} for key, value in options.items()]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.member_id))
        await self.drain()

    def last(self) -> dict:
        """The newest message the bot sent, as Discord stored it."""
        return self.net.posted[-1]

    def last_text(self) -> str:
        return self.last().get("content") or ""

    def last_card(self) -> dict:
        return self.last()["embeds"][0]

    def answered(self, *texts: str) -> None:
        """The model answers the next requests: one search, then these words."""
        for words in texts:
            self.model.reply(self.model.tool("search_wiki", {"query": "nozzle"}), self.model.text(words))

    async def uses(self, user_id: int | None = None) -> int:
        return await self.db.scalar("SELECT COALESCE(SUM(uses), 0) FROM answer_uses WHERE guild_id = ? AND user_id = ?", (self.guild_id, user_id or self.member_id))

    async def costs(self) -> list[dict]:
        return await self.db.all("SELECT * FROM answer_costs WHERE guild_id = ? ORDER BY id", (self.guild_id,))


class CopyAtSetupTest(AnswersCase):
    async def test_naming_a_source_builds_the_copy_the_commands_read(self):
        index = self.answers.copies.index(self.source())
        self.assertEqual(sorted(index.pages), [BASE + "/en/nozzle", BASE + "/en/plate"])

    async def test_a_server_that_turns_the_pack_off_lets_go_of_the_index(self):
        source = self.source()
        await self.configure(enabled=False)
        self.assertIsNone(self.answers.copies.index(source))
        await self.configure(enabled=True)
        self.assertIsNotNone(self.answers.copies.index(source))
        self.assertEqual(len(self.web.called(SITEMAP)), 1, "the stored copy is loaded again, not downloaded")


class HowDoITest(AnswersCase):
    async def test_an_answer_goes_on_a_card_with_its_sources_and_footer(self):
        self.answered("Let the hotend cool. [S1] Then unscrew it. [S2]")
        await self.slash("how_do_i", question=QUESTION)
        card = self.last_card()
        self.assertEqual(card["title"], QUESTION)
        self.assertEqual(card["description"], "Let the hotend cool. [1] Then unscrew it. [1]")
        self.assertEqual(card["fields"], [{"name": "Sources", "value": f"[1] [Replacing the Nozzle]({BASE}/en/nozzle)", "inline": False}])
        self.assertEqual(card["footer"]["text"], "Answered from the Sample Wiki. Check the sources before you act on it.")
        first = self.model.requests[0]
        self.assertEqual((first["model"], first["output_config"], first["max_tokens"], first["stream"]), ("claude-haiku-5-5", {"effort": "high"}, 64000, True))
        self.assertEqual(first["system"], prompts.fill(prompts.HOW_DO_I, WORDS) + "\n\n" + prompts.fill(prompts.RULES, WORDS))
        self.assertIn("Answer only from the Sample Wiki, which you reach", first["system"])
        self.assertEqual(first["messages"][0], {"role": "user", "content": QUESTION})
        self.assertEqual([tool["name"] for tool in first["tools"]], ["search_wiki", "read_wiki_page"])
        self.assertTrue(first["tools"][0]["description"].startswith("Keyword search over the Sample Wiki."))
        self.assertEqual(await self.uses(), 1)
        [row] = await self.costs()
        self.assertEqual((row["command"], row["status"], row["model"], row["user_id"], row["rounds"]), ("how_do_i", "ok", "claude-haiku-5-5", self.member_id, 2))
        self.assertAlmostEqual(row["cost"], (2000 * 0.10 + 200 * 0.50) / 1_000_000)
        self.assert_no_errors()

    async def test_an_answer_near_the_end_of_the_reply_window_goes_to_the_channel_naming_the_person(self):
        self.answered("Unscrew it. [S2]")
        with mock.patch.object(pack, "LATE_REPLY_MARGIN", datetime.timedelta(minutes=16)):  # every reply counts as late
            await self.slash("how_do_i", question=QUESTION)
        method, path, body = next(call for call in reversed(self.net.calls) if call[0] == "POST" and call[1] == "/channels/{channel_id}/messages")
        self.assertEqual(body["content"], f"<@{self.member_id}>")
        self.assertEqual((body["allowed_mentions"]["parse"], [int(user) for user in body["allowed_mentions"]["users"]]), ([], [self.member_id]), "only the person is pinged")
        self.assertEqual(body["embeds"][0]["title"], QUESTION)
        self.assertEqual(int(self.last()["channel_id"]), int(self.text["id"]))
        self.assert_no_errors()

    async def test_a_typed_question_gets_the_same_card(self):
        self.answered("Unscrew it. [S2]")
        self.dispatch_message(user_id=self.member_id, content=f"!how_do_i {QUESTION}")
        await self.drain()
        self.assertEqual(self.last_card()["title"], QUESTION)
        self.assertEqual(int(self.last()["channel_id"]), int(self.text["id"]))

    async def test_the_servers_subject_rules_footer_and_model_are_used(self):
        subject = {**prompts.SUBJECT, "community": "the Example community", "maker": "Example", "search_example": "E1 nozzle"}
        await self.configure(subject=subject, rules="Stay on {wiki}.", models={"how_do_i": "claude-sonnet-5-5", "brainstorm": "claude-sonnet-5-5", "ask": "claude-sonnet-5-5", "make_model": "claude-sonnet-5-5"},
                             footers={"how_do_i": "", "brainstorm": "b", "ask": "a", "make_model": "m"})
        self.answered("Unscrew it. [S1]")
        await self.slash("how_do_i", question=QUESTION)
        first = self.model.requests[0]
        self.assertEqual(first["model"], "claude-sonnet-5-5")
        self.assertTrue(first["system"].startswith("You answer questions from members of the Example community about the products its wiki covers."))
        self.assertIn("Questions that aren't about Example products get one sentence", first["system"])
        self.assertTrue(first["system"].endswith("\n\nStay on the Sample Wiki."))
        self.assertEqual(first["tools"][0]["input_schema"]["properties"]["query"]["description"], "Search terms, for example: E1 nozzle")
        self.assertNotIn("footer", self.last_card(), "an empty footer is left off")


class CopiesInUseTest(AnswersCase):
    async def test_an_update_stops_when_its_source_is_dropped(self):
        asked = []
        real = self.answers.copies.refresh

        async def watched(source, outside, **kwargs):
            asked.append(kwargs.get("wanted"))
            return await real(source, outside, **kwargs)
        with mock.patch.object(self.answers.copies, "refresh", watched):
            await self.slash("refresh_wiki", user_id=self.admin_id)
        [wanted] = asked
        self.assertTrue(wanted())
        await self.configure(sources=[DOCS_ROW])
        self.assertFalse(wanted(), "the wiki left the server's list, so its update stops")


class SeveralSourcesTest(AnswersCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.configure(sources=[WIKI_ROW, DOCS_ROW])

    async def test_a_crawled_source_joins_the_search_and_answers_name_what_they_drew_from(self):
        self.assertEqual(sorted(self.answers.copies.index(self.source(1)).pages), [DOCS + "/guide", DOCS + "/guide/wear"])
        self.model.reply(self.model.tool("search_wiki", {"query": "worn nozzle"}), self.model.text("A worn nozzle under-extrudes. [S1]"))
        await self.slash("how_do_i", question="Why does my worn nozzle under-extrude?")
        first = self.model.requests[0]
        self.assertIn("Answer only from the Sample Wiki and the Sample Docs, which you reach", first["system"])
        self.assertIn("(the Sample Docs)", self.model.tool_results(1)[0]["content"])
        card = self.last_card()
        self.assertEqual(card["fields"][0]["value"], f"[1] [Nozzle wear]({DOCS}/guide/wear)")
        self.assertEqual(card["footer"]["text"], footer("how_do_i", "the Sample Docs"), "the footer names only the source the answer drew from")

    async def test_an_answer_citing_nothing_names_every_source(self):
        self.model.reply(self.model.text("Check the store."))
        await self.slash("ask", question="Which filament holds up best outdoors?")
        self.assertEqual(self.last_card()["footer"]["text"], footer("ask", "the Sample Wiki and the Sample Docs"))
        self.assertEqual(self.model.requests[0]["tools"][-1]["blocked_domains"], ["wiki.example.org", "docs.example.org"])

    async def test_one_source_still_building_leaves_the_others_answering(self):
        self.answers.copies.indexes.pop(self.source(1).key)
        self.answered("Unscrew it. [S1]")
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_card()["description"], "Unscrew it. [1]")

    async def test_each_source_updates_on_its_own_schedule(self):
        start = time.time()
        await self.answers.refresh_due_sources(now=start + 13 * 3600)
        self.assertEqual(len(self.web.called(SITEMAP)), 1, "a day hasn't passed for the wiki")
        self.assertEqual(len(self.web.called(DOCS + "/guide/wear")), 2, "twelve hours have for the docs")


class OtherCommandsTest(AnswersCase):
    async def test_brainstorm_plans_on_its_own_model_with_the_sketch_tool(self):
        if not pack.engine.sketches_available():
            self.skipTest("The cairo library isn't on this machine.")
        self.model.reply(self.model.tool("draw_sketch", {"svg": SVG, "caption": "The bracket"}), self.model.text("Print it flat. [S9]"))
        await self.slash("brainstorm", idea="A wall bracket for my spool holder, please draw it")
        first = self.model.requests[0]
        self.assertEqual((first["model"], first["output_config"]["effort"]), ("claude-sonnet-5-5", "high"))
        self.assertEqual(first["system"], prompts.fill(prompts.BRAINSTORM, WORDS) + "\n\n" + prompts.fill(prompts.RULES, WORDS))
        self.assertEqual([tool["name"] for tool in first["tools"]], ["search_wiki", "read_wiki_page", "draw_sketch"])
        card = self.last_card()
        self.assertEqual(card["image"]["url"], "attachment://sketch.png")
        self.assertEqual(card["fields"][-1], {"name": "Sketch", "value": "The bracket", "inline": False})
        self.assertEqual(card["footer"]["text"], footer("brainstorm"))
        sent = [body for method, path, body in self.net.calls if method == "POST" and "attachments" in (body or {})]
        self.assertEqual([part["filename"] for part in sent[-1]["attachments"]], ["sketch.png"])

    async def test_brainstorm_runs_at_most_six_rounds(self):
        for _ in range(6):
            self.model.reply(self.model.tool("search_wiki", {"query": "x"}))
        await self.slash("brainstorm", idea="A wall bracket for my spool holder")
        self.assertEqual(len(self.model.requests), 6)
        self.assertEqual(self.model.requests[5]["tool_choice"], {"type": "none"})

    async def test_ask_searches_the_web_but_not_the_sources_sites(self):
        self.model.reply(self.model.text("Check the shop."))
        await self.slash("ask", question="Which filament holds up best outdoors?")
        first = self.model.requests[0]
        self.assertEqual(first["model"], "claude-sonnet-5-5")
        self.assertEqual(first["system"], prompts.fill(prompts.ASK, WORDS) + "\n\n" + prompts.fill(prompts.RULES, WORDS))
        self.assertEqual(first["tools"][-1], {"type": "web_search_20260209", "name": "web_search", "max_uses": 5, "blocked_domains": ["wiki.example.org"]})
        self.assertEqual(self.last_card()["footer"]["text"], footer("ask"))

    async def test_ask_doesnt_send_questions_elsewhere(self):
        self.model.reply(self.model.text("ASK"))
        await self.slash("ask", question="Which printer should I buy?")
        self.assertEqual(self.last_card()["description"], "ASK")

    async def test_make_model_posts_the_model_its_source_and_preview(self):
        self.model.reply(self.model.tool("render_model", {"name": "Spool Clip", "scad": "cube([40, 20, 5.5]);"}), self.model.text("A clip, 40 mm wide."))
        with mock.patch.object(modelmaker, "available", lambda: True), mock.patch.object(modelmaker, "run_held", OpenScad()):
            await self.slash("make_model", description="A clip that holds the end of a filament spool")
        first = self.model.requests[0]
        self.assertEqual((first["model"], first["output_config"]["effort"], first["max_tokens"]), ("claude-sonnet-5-5", "medium", 64000))
        self.assertEqual(first["system"], prompts.fill(prompts.MAKE_MODEL, WORDS) + "\n\n" + prompts.fill(prompts.RULES, WORDS))
        self.assertEqual([tool["name"] for tool in first["tools"]], ["search_wiki", "read_wiki_page", "render_model"])
        self.assertIn("Rendered. Size 40.0 x 20.0 x 5.5 mm", self.model.tool_results(1)[0]["content"])
        card = self.last_card()
        self.assertEqual(card["fields"][-1], {"name": "Model", "value": "spool-clip.3mf, 40.0 x 20.0 x 5.5 mm", "inline": False})
        self.assertEqual(card["image"]["url"], "attachment://preview.png")
        self.assertEqual(card["footer"]["text"], footer("make_model"))
        sent = [body for method, path, body in self.net.calls if method == "POST" and "attachments" in (body or {})]
        self.assertEqual([part["filename"] for part in sent[-1]["attachments"]], ["preview.png", "spool-clip.3mf", "spool-clip.scad"])

    async def test_make_model_renders_at_most_four_times(self):
        blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}]
        blocks += [{"type": "tool_use", "id": f"toolu_render_{number}", "name": "render_model", "input": {"name": "Clip", "scad": "cube([40, 20, 5.5]);"}} for number in range(6)]
        self.model.reply(self.model.message(blocks, "tool_use"), self.model.text("A clip."))
        programs = OpenScad()
        with mock.patch.object(modelmaker, "available", lambda: True), mock.patch.object(modelmaker, "run_held", programs):
            await self.slash("make_model", description="A clip that holds the end of a filament spool")
        self.assertEqual(len([command for command in programs.commands if "model.stl" in command]), 4)
        self.assertEqual(modelmaker.MAX_RENDERS, 4)

    async def test_make_model_without_openscad_says_so_and_costs_nothing(self):
        with mock.patch.object(modelmaker, "available", lambda: False):
            await self.slash("make_model", description="A clip that holds the end of a filament spool")
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.NO_MODELS)
        self.assertEqual((self.model.requests, await self.uses()), ([], 0))


class NotReadyTest(AnswersCase):
    async def test_no_key_no_wiki_and_a_copy_still_building(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.NO_KEY)
        self.answers.copies.indexes.clear()
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.BUILDING)
        await self.bot.settings.save(self.guild_id, "answers", {"sources": []})
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.NO_WIKI)
        self.assertEqual((self.model.requests, await self.uses()), ([], 0))

    async def test_a_pack_thats_off_refuses_typed_commands(self):
        await self.configure(enabled=False)
        self.dispatch_message(user_id=self.member_id, content=f"!how_do_i {QUESTION}")
        await self.drain()
        self.assertIn("switched off in this server", self.last_text())
        self.assertEqual(self.model.requests, [])


class LimitsTest(AnswersCase):
    async def test_a_persons_daily_number_is_shared_by_the_commands_and_kept(self):
        await self.configure(daily_limit=2)
        self.answered("One. [S1]", "Two. [S1]")
        await self.slash("how_do_i", question=QUESTION)
        await self.slash("how_do_i", question=QUESTION)
        await self.slash("ask", question="Which filament holds up best outdoors?")
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.DAILY.format(limit=2))
        self.assertEqual(len(self.model.requests), 4, "the third request never reached the model")
        self.assertEqual(await self.uses(), 2)
        self.answers._bursts.clear()  # a restart forgets the minute, and the day's count stays in the database
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.DAILY.format(limit=2))
        self.answered("Three. [S1]")
        await self.slash("how_do_i", question=QUESTION, user_id=self.mod_id)
        self.assertEqual(self.last_card()["description"], "Three. [1]", "the limit is each person's own")

    async def test_a_server_past_its_monthly_budget_gets_no_more_answers(self):
        await self.configure(monthly_limit=5)
        await self.db.run("INSERT INTO answer_costs (guild_id, command, status, model, cost, at) VALUES (?, 'ask', 'ok', 'm', 5.0, ?)", (self.guild_id, time.time()))
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.MONTH_SPENT.format(limit=5))
        self.assertEqual((self.model.requests, await self.uses()), ([], 0))

    async def test_a_budget_of_zero_means_none(self):
        await self.configure(monthly_limit=0)
        await self.db.run("INSERT INTO answer_costs (guild_id, command, status, model, cost, at) VALUES (?, 'ask', 'ok', 'm', 5000.0, ?)", (self.guild_id, time.time()))
        self.answered("Unscrew it. [S1]")
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_card()["description"], "Unscrew it. [1]")

    async def test_nothing_is_posted_where_the_member_cant_post(self):
        allowed = discord.Permissions(view_channel=True, read_message_history=True, use_application_commands=True)
        self.bot._connection.parse_guild_role_update({"guild_id": str(self.guild_id), "role": {**self.role_everyone, "permissions": str(allowed.value)}})
        self.answered("Gift cards aren't covered. [S1]")
        await self.slash("how_do_i", question="Free gift cards for everyone who messages me today")
        self.assertEqual(self.last_text(), "\N{CROSS MARK} You can't post in that channel.")
        self.assertFalse(any(message.get("embeds") for message in self.net.posted))
        self.assertEqual((len(self.model.requests), await self.uses()), (0, 0))

    async def test_three_a_minute(self):
        self.answered("One. [S1]", "Two. [S1]", "Three. [S1]")
        for _ in range(3):
            await self.slash("how_do_i", question=QUESTION)
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.BURST)
        self.assertEqual(await self.uses(), 3)

    async def test_failures_before_billing_give_the_slot_back(self):
        await self.slash_with(self.model.error(429, "rate_limit_error", "slow"))
        self.assertEqual(self.last_text(), pack.STATUS_TEXT["busy"] + " " + pack.NOT_COUNTED)
        await self.slash_with(self.model.error(500, "api_error", "down"))
        self.assertEqual(self.last_text(), pack.STATUS_TEXT["unavailable"] + " " + pack.NOT_COUNTED)
        await self.slash_with(self.model.error(401, "authentication_error", "bad key"))
        self.assertEqual(self.last_text(), pack.STATUS_TEXT["key_refused"] + " " + pack.NOT_COUNTED)
        self.answers._bursts.clear()
        await self.slash_with(self.model.error(404, "not_found_error", "model: no-such-model"))
        self.assertEqual(self.last_text(), pack.STATUS_TEXT["error"] + " " + pack.NOT_COUNTED)
        self.assertEqual(await self.uses(), 0)
        self.assertEqual([row["cost"] for row in await self.costs()], [0, 0, 0, 0])

    async def test_a_billed_request_counts_however_it_ends(self):
        await self.slash_with(self.model.message([{"type": "thinking", "thinking": "", "signature": "s"}], "max_tokens", output_tokens=16000))
        self.assertEqual(self.last_text(), pack.STATUS_TEXT["too_long"])
        self.model.reply(self.model.tool("search_wiki", {"query": "nozzle"}), self.model.error(529, "overloaded_error", "busy"))
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), pack.STATUS_TEXT["unavailable"], "the first round was billed")
        self.assertEqual(await self.uses(), 2)
        self.assertNotIn("count", pack.STATUS_TEXT["too_long"] + pack.STATUS_TEXT["unavailable"])

    async def test_answers_the_model_gave_still_count(self):
        await self.slash_with(self.model.text("DECLINE"))
        self.assertEqual(self.last_text(), pack.RULES_NOTICE)
        await self.slash_with(self.model.text("", stop_reason="refusal"))
        self.assertEqual(self.last_text(), pack.STATUS_TEXT["refused"])
        self.assertEqual(await self.uses(), 2)
        self.assertEqual([row["status"] for row in await self.costs()], ["declined", "refused"])

    async def test_a_question_for_ask_is_sent_there_and_doesnt_count(self):
        async def registered(*, guild=None):
            return [type("Command", (), {"name": "ask", "mention": "</ask:42>"})()]
        with mock.patch.object(self.bot.tree, "fetch_commands", registered):
            await self.slash_with(self.model.text("ASK"))
        self.assertEqual(self.last_text(), pack.REDIRECT.format(ask="</ask:42>") + " " + pack.NOT_COUNTED)
        self.assertEqual(await self.uses(), 0)

    async def test_only_the_first_few_redirects_a_day_are_free(self):
        async def none_listed(*, guild=None):
            return []
        with mock.patch.object(self.bot.tree, "fetch_commands", none_listed):
            for _ in range(pack.REDIRECTS_FREE + 1):
                self.answers._bursts.clear()
                await self.slash_with(self.model.text("ASK"))
        self.assertEqual(self.last_text(), pack.REDIRECT.format(ask="/ask"), "past the free ones it counts, and says nothing about the limit")
        self.assertEqual(await self.uses(), 1)
        self.assertEqual(pack.REDIRECTS_FREE, 3)

    async def test_the_redirect_names_ask_plainly_when_discord_wont_list_the_commands(self):
        async def refused(*, guild=None):
            raise discord.Forbidden(type("Response", (), {"status": 403, "reason": "no"})(), "Missing Access")
        with mock.patch.object(self.bot.tree, "fetch_commands", refused):
            await self.slash_with(self.model.text("ASK"))
        self.assertEqual(self.last_text(), pack.REDIRECT.format(ask="/ask") + " " + pack.NOT_COUNTED)

    async def slash_with(self, reply) -> None:
        self.model.reply(reply)
        await self.slash("how_do_i", question=QUESTION)


class HostListTest(AnswersCase):
    """Each answer is billed to the host's key, so the host names the servers that may use it. Anywhere else the pack does nothing."""

    async def test_a_server_off_the_hosts_list_has_no_commands_answers_or_copies(self):
        servers.use(set())
        self.assertFalse(self.answers.on_in(self.guild_id))
        await self.bot.sync_server_commands(self.guild_id)
        names = {command.name for command in self.bot.tree.get_commands(guild=discord.Object(self.guild_id))}
        self.assertTrue(names.isdisjoint({"how_do_i", "brainstorm", "ask", "make_model", "refresh_wiki", "auto_answer"}), names)
        self.dispatch_message(user_id=self.member_id, content=f"!how_do_i {QUESTION}")
        await self.drain()
        self.assertIn("switched off in this server", self.last_text())
        self.assertEqual(self.answers.sources_in_use(), {})
        self.assertEqual(self.model.requests, [])

    async def test_the_list_is_read_from_the_start_up_option(self):
        self.assertEqual(servers.parse("1100000000000000001, 1100000000000000002 1100000000000000003"),
                         {1100000000000000001, 1100000000000000002, 1100000000000000003})
        with self.assertRaisesRegex(ValueError, "abc isn't a server ID"):
            servers.parse("1100000000000000001,abc")
        root = Path(__file__).resolve().parent.parent
        run = subprocess.run([sys.executable, "-m", "captainguild", "--public-url", "https://x.example", "--answers-servers", "abc"],
                             cwd=root, capture_output=True, text=True, timeout=120, env={**os.environ, "DISCORD_TOKEN": "", "MASTER_KEY": ""})
        self.assertEqual(run.returncode, 2)
        self.assertIn("--answers-servers: abc isn't a server ID", run.stderr)


class AutomodScreenTest(AnswersCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["badword"], "punishment": {"actions": 1}}})

    async def test_a_request_the_word_rule_flags_never_reaches_the_model(self):
        await self.slash("how_do_i", question="How do I print a badword sign?")
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.RULES_NOTICE)
        self.assertEqual((self.model.requests, await self.uses()), ([], 0))

    async def test_an_answer_the_rule_flags_isnt_posted_since_it_cant_be_censored(self):
        self.answered("Paint a badword on it. [S1]")
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), pack.RULES_NOTICE)
        self.assertEqual([row["status"] for row in await self.costs()], ["blocked_reply"])
        self.assertEqual(await self.uses(), 1, "a held-back answer was billed, so it counts")

    async def test_staff_get_the_same_screen_as_anyone(self):
        # The model writes the answer, so whoever asked, it's screened the way a member's message would be, as the old bot screened it.
        await self.slash("how_do_i", question="How do I print a badword sign?", user_id=self.mod_id)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.RULES_NOTICE)
        self.answered("Paint a badword on it. [S1]")
        await self.slash("how_do_i", question=QUESTION, user_id=self.mod_id)
        self.assertEqual(self.last_text(), pack.RULES_NOTICE)
        self.assertEqual([row["status"] for row in await self.costs()], ["blocked_reply"])


class UnlistedServerScreenTest(AnswersCase):
    """The host's word lists name other servers only. This one keeps the screen it had: its automod word rule."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(holder.cleanup)
        module, configs = write_term_lists(Path(holder.name), {self.guild_id + 1: {"blacklists": {"House": ["otherword"]}, "whitelists": {}}})
        screening.use(screening.load(module, configs))
        await self.bot.settings.save(self.guild_id, "automod", {"words": {"enabled": True, "entries": ["badword"], "punishment": {"actions": 1}}})

    async def test_a_server_the_lists_dont_name_keeps_its_automod_screen(self):
        await self.slash("how_do_i", question="How do I print a badword sign?")
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.RULES_NOTICE)
        self.answered("Paint a badword on it. [S1]")
        await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), pack.RULES_NOTICE)
        self.assertEqual([row["status"] for row in await self.costs()], ["blocked_reply"])


class TermListsScreenTest(AnswersCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.holder.cleanup)
        module, configs = write_term_lists(Path(self.holder.name), {self.guild_id: {"blacklists": {"House": ["badword"]}, "whitelists": {}, "replacement_string": "[x]"}})
        screening.use(screening.load(module, configs))

    async def test_a_listed_word_stops_the_request_for_everyone(self):
        await self.slash("how_do_i", question="How do I print a badw0rd sign?", user_id=self.owner_id)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.RULES_NOTICE)
        self.assertEqual(self.model.requests, [])

    async def test_the_card_is_censored_like_a_members_message(self):
        self.answered("Paint a badword on it. [S1]")
        with mock.patch.object(pack, "REQUEST_SCREEN_SECONDS", 0.0):  # a check that runs out of time lets the request through
            await self.slash("how_do_i", question="Can I print a badword sign?")
        card = self.last_card()
        self.assertEqual((card["title"], card["description"]), ("Can I print a [x] sign?", "Paint a [x] on it. [1]"))

    async def test_a_screen_that_fails_holds_the_answer_and_keeps_its_cost(self):
        module = Path(self.holder.name) / "term_matching.py"
        module.write_text(module.read_text(encoding="utf-8").replace(
            "async def censor_terms(content, blacklists, whitelists, replacement):",
            "async def censor_terms(content, blacklists, whitelists, replacement):\n    raise RuntimeError('a filter fault')"), encoding="utf-8")
        screening.use(screening.load(module, Path(self.holder.name) / "server_configs"))
        self.answered("Unscrew it. [S1]")
        with self.assertLogs("captainguild.modules.answers", level="ERROR"):
            await self.slash("how_do_i", question=QUESTION)
        self.assertEqual(self.last_text(), pack.RULES_NOTICE)
        self.assertEqual([row["status"] for row in await self.costs()], ["blocked_reply"], "the billed answer keeps its row")
        self.assertEqual(await self.uses(), 1)

    # Models whose source doesn't show the listed word as written, though OpenSCAD draws it.
    DRAWN_WORDS = [
        r'linear_extrude(height = 1) text("b\x61dword");',
        "linear_extrude(height = 1) text(chr(98, 97, 100, 119, 111, 114, 100));",
        'linear_extrude(height = 1) text(str("bad", "word"));',
        'w = "word";\nlinear_extrude(height = 1) text(str("bad", w));',
    ]

    async def test_a_listed_word_a_model_draws_stops_the_post(self):
        for scad in self.DRAWN_WORDS:
            with self.subTest(scad=scad):
                self.answers._bursts.clear()
                self.model.reply(self.model.tool("render_model", {"name": "Sign", "scad": scad}), self.model.text("A sign."))
                with mock.patch.object(modelmaker, "available", lambda: True), mock.patch.object(modelmaker, "run_held", OpenScad()):
                    await self.slash("make_model", description="A sign for my workshop door")
                self.assertEqual(self.last_text(), pack.RULES_NOTICE)
                self.assertEqual((await self.costs())[-1]["status"], "blocked_reply")

    async def test_a_listed_word_a_drawing_splits_with_tags_stops_the_post(self):
        if not pack.engine.sketches_available():
            self.skipTest("The cairo library isn't on this machine.")
        for word in ["bad<tspan>word</tspan>", "b<a href='#x'>ad</a>word", "bad\nword"]:
            with self.subTest(word=word):
                self.answers._bursts.clear()
                drawing = SVG.replace("Tom &amp; Jerry", word)
                self.model.reply(self.model.tool("draw_sketch", {"svg": drawing, "caption": "x"}), self.model.text("Here. [S1]"))
                await self.slash("brainstorm", idea="A sign for my workshop door, with a drawing")
                self.assertEqual(self.last_text(), pack.RULES_NOTICE)
                self.assertEqual((await self.costs())[-1]["status"], "blocked_reply")

    async def test_a_listed_word_a_drawing_spells_with_character_references_stops_the_post(self):
        if not pack.engine.sketches_available():
            self.skipTest("The cairo library isn't on this machine.")
        for word in ["b&#97;dword", "b&#x61;dword", "&#98;&#x61;dword"]:
            with self.subTest(word=word):
                self.answers._bursts.clear()
                drawing = SVG.replace("Tom &amp; Jerry", word)
                self.model.reply(self.model.tool("draw_sketch", {"svg": drawing, "caption": "x"}), self.model.text("Here. [S1]"))
                await self.slash("brainstorm", idea="A sign for my workshop door, with a drawing")
                self.assertEqual(self.last_text(), pack.RULES_NOTICE)
                self.assertEqual((await self.costs())[-1]["status"], "blocked_reply")

    async def test_a_listed_word_in_a_drawing_stops_the_post(self):
        if not pack.engine.sketches_available():
            self.skipTest("The cairo library isn't on this machine.")
        drawing = SVG.replace("Tom &amp; Jerry", "badword")
        self.model.reply(self.model.tool("draw_sketch", {"svg": drawing, "caption": "x"}), self.model.text("Here. [S1]"))
        await self.slash("brainstorm", idea="A sign for my workshop door, with a drawing")
        self.assertEqual(self.last_text(), pack.RULES_NOTICE)
        self.assertEqual([row["status"] for row in await self.costs()], ["blocked_reply"])


class AutoAnswerTest(AnswersCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.staff_tag = new_id()
        forum = {**self.forum, "available_tags": [{"id": str(self.staff_tag), "name": "Guide", "moderated": True, "emoji_id": None, "emoji_name": None}]}
        self.bot._connection.parse_channel_update(forum)
        await self.configure(auto_answer_forums=[int(self.forum["id"])])

    def post(self, title: str, content: str, *, tags=(), user_id: int | None = None, attachments: int = 0, forum: dict | None = None) -> int:
        thread_id = new_id()
        author = user_id or self.member_id
        self.bot._connection.parse_thread_create({
            "id": str(thread_id), "guild_id": str(self.guild_id), "parent_id": (forum or self.forum)["id"], "owner_id": str(author), "name": title, "type": 11,
            "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "applied_tags": [str(tag) for tag in tags], "flags": 0,
            "thread_metadata": {"archived": False, "auto_archive_duration": 4320, "archive_timestamp": iso(), "locked": False}})
        files = [{"id": str(new_id()), "filename": f"p{n}.png", "size": 1, "url": f"https://cdn.example/p{n}.png", "proxy_url": f"https://cdn.example/p{n}.png"} for n in range(attachments)]
        self.dispatch_message(user_id=author, content=content, channel={"id": str(thread_id)}, id=str(thread_id), attachments=files)
        return thread_id

    async def test_a_new_post_gets_one_answer_with_the_line_above(self):
        self.answered("Let it cool first. [S1]")
        thread = self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?", attachments=2)
        await self.drain()
        reply = self.last()
        self.assertEqual(int(reply["channel_id"]), thread)
        self.assertEqual(reply["content"], "While you wait for a helper, here's what the Sample Wiki says. If it doesn't solve the problem, a helper will follow up.")
        self.assertEqual(reply["embeds"][0]["title"], "Nozzle clogged")
        self.assertEqual(reply["embeds"][0]["footer"]["text"], footer("how_do_i"))
        body = [body for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages"][-1]
        self.assertEqual(int(body["message_reference"]["message_id"]), thread)
        self.assertEqual(body["allowed_mentions"], {"parse": [], "replied_user": False})
        first = self.model.requests[0]
        self.assertEqual(first["system"], "\n\n".join((prompts.fill(prompts.HOW_DO_I, WORDS), prompts.fill(prompts.RULES, WORDS), prompts.fill(prompts.AUTO_NOTE, WORDS))))
        self.assertEqual(first["messages"][0]["content"], "Nozzle clogged: My nozzle is clogged after printing PETG, how do I clear it? (The member attached 2 photo or video files that you can't see.)")
        [row] = await self.costs()
        self.assertEqual((row["command"], row["user_id"]), ("auto_answer", None))
        self.assert_no_errors()

    async def test_a_post_is_answered_once(self):
        self.answered("Let it cool first. [S1]")
        thread = self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain()
        starter = self.message_payload(user_id=self.member_id, content="My nozzle is clogged after printing PETG, how do I clear it?", channel={"id": str(thread)}, id=str(thread))
        await self.answers.answer_post(discord.Message(state=self.bot._connection, channel=self.guild.get_thread(thread), data=starter))
        self.assertEqual(len(self.model.requests), 2, "the second delivery of the same post asked nothing")

    async def test_posts_left_to_the_helpers(self):
        other_forum = {**self.forum, "id": str(new_id()), "name": "gallery"}
        self.bot._connection.parse_channel_create(other_forum)
        self.post("Look", "My new print came out great today everyone")
        self.model.reply(self.model.text("SKIP"))  # the post above reached the model, which skipped it
        cases = [
            lambda: self.post("Guide", "How to clear a clogged nozzle in five steps", tags=[self.staff_tag]),
            lambda: self.post("Help", "what is this"),
            lambda: self.post("Clogged nozzle again", "It clogs every time I print PETG at 250", forum=other_forum),
            lambda: self.post("A bot post", "It clogs every time I print PETG at 250", user_id=self.bot_id),
        ]
        for make in cases:
            make()
            await self.drain()
        self.assertEqual(len(self.model.requests), 1)
        self.assertFalse(any(message.get("embeds") for message in self.net.posted))

    async def test_an_answer_that_cites_nothing_isnt_posted(self):
        self.model.reply(self.model.text("The wiki doesn't cover this."))
        self.post("Odd noise", "My printer makes an odd noise when it homes the bed")
        await self.drain()
        self.assertFalse(any(message.get("embeds") for message in self.net.posted))

    async def test_the_daily_number_for_the_server(self):
        await self.configure(auto_answer_daily_limit=1)
        self.answered("Let it cool first. [S1]")
        self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain()
        self.post("Plate dirty", "My build plate is dirty and prints don't stick anymore")
        await self.drain()
        self.assertEqual(len(self.model.requests), 2, "one answer, two rounds")
        self.assertEqual(await self.db.scalar("SELECT answers FROM answer_auto_days WHERE guild_id = ?", (self.guild_id,)), 1)

    async def test_automatic_answers_with_no_budget(self):
        await self.configure(monthly_limit=0)
        await self.db.run("INSERT INTO answer_costs (guild_id, command, status, model, cost, at) VALUES (?, 'ask', 'ok', 'm', 5000.0, ?)", (self.guild_id, time.time()))
        self.answered("Let it cool first. [S1]")
        self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain()
        self.assertEqual(self.last()["embeds"][0]["description"], "Let it cool first. [1]")

    async def test_no_automatic_answers_past_the_monthly_budget(self):
        await self.configure(monthly_limit=1)
        await self.db.run("INSERT INTO answer_costs (guild_id, command, status, model, cost, at) VALUES (?, 'ask', 'ok', 'm', 1.0, ?)", (self.guild_id, time.time()))
        self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain()
        self.assertEqual(self.model.requests, [])

    async def test_nothing_happens_without_message_content_or_with_the_pack_off(self):
        self.bot.reads_message_text = False
        self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain()
        self.bot.reads_message_text = True
        await self.configure(enabled=False)
        self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain()
        self.assertEqual(self.model.requests, [])

    async def test_a_listed_word_is_censored_in_an_automatic_answer(self):
        holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(holder.cleanup)
        module, configs = write_term_lists(Path(holder.name), {self.guild_id: {"blacklists": {"House": ["badword"]}, "whitelists": {}}})
        screening.use(screening.load(module, configs))
        self.answered("Never say badword. [S1]")
        self.post("Nozzle clogged", "My nozzle is clogged after printing PETG, how do I clear it?")
        await self.drain()
        self.assertEqual(self.last()["embeds"][0]["description"], "Never say ***. [1]")


class AdminCommandsTest(AnswersCase):
    def forum_option(self) -> dict:
        return {"channels": {self.forum["id"]: {"id": self.forum["id"], "name": self.forum["name"], "type": 15, "permissions": str(discord.Permissions.all().value),
                                                 "parent_id": self.forum["parent_id"], "guild_id": str(self.guild_id), "nsfw": False, "position": 4, "flags": 0}}}

    async def test_auto_answer_turns_a_forum_on_and_off(self):
        await self.slash("auto_answer", user_id=self.admin_id, resolved=self.forum_option(), forum=self.forum["id"], enabled=True)
        self.assertEqual(self.settings()["auto_answer_forums"], [int(self.forum["id"])])
        self.assertEqual(self.last_text(), f"Automatic answers are now **on** in <#{self.forum['id']}>.\nForums with automatic answers: <#{self.forum['id']}>.")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            await self.slash("auto_answer", user_id=self.admin_id, resolved=self.forum_option(), forum=self.forum["id"], enabled=True)
        self.assertIn(pack.NO_KEY + " Nothing will be posted until then.", self.last_text())
        await self.slash("auto_answer", user_id=self.admin_id, resolved=self.forum_option(), forum=self.forum["id"], enabled=False)
        self.assertEqual(self.settings()["auto_answer_forums"], [])
        self.assertTrue(self.last_text().endswith("Forums with automatic answers: none."))
        recent = await self.bot.activity_log.recent(self.guild_id)
        self.assertEqual(recent[0]["action"], "answers.auto_answer")

    async def test_only_an_administrator_changes_what_answers_cost(self):
        await self.slash("auto_answer", user_id=self.mod_id, resolved=self.forum_option(), forum=self.forum["id"], enabled=True)
        self.assertIn("You need Administrator", self.last_text())
        await self.bot.settings.save(self.guild_id, "commands", {"mod_role": int(self.role_mod["id"])})
        await self.slash("auto_answer", user_id=self.mod_id, resolved=self.forum_option(), forum=self.forum["id"], enabled=True)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} Only the server owner or an Administrator can change this.")
        await self.slash("refresh_wiki", user_id=self.mod_id)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} Only the server owner or an Administrator can change this.")
        self.assertEqual(self.settings()["auto_answer_forums"], [])

    async def test_refresh_wiki_reports_what_it_did(self):
        await self.slash("refresh_wiki", user_id=self.admin_id)
        self.assertRegex(self.last_text(), r"^Wiki updated in [0-9.]+s: 0 pages downloaded, 2 unchanged, 0 removed, 0 failed\. The copy now holds 2 pages\.$")
        self.web.answer(SITEMAP, "", status=500)
        await self.slash("refresh_wiki", user_id=self.admin_id)
        self.assertEqual(self.last_text(), "The wiki update failed. The sitemap answered with HTTP 500.")
        await self.bot.settings.save(self.guild_id, "answers", {"sources": []})
        await self.slash("refresh_wiki", user_id=self.admin_id)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.NO_WIKI)

    async def test_refresh_wiki_names_each_source_when_there_are_several(self):
        await self.configure(sources=[WIKI_ROW, DOCS_ROW])
        await self.slash("refresh_wiki", user_id=self.admin_id)
        first, second = self.last_text().split("\n")
        self.assertTrue(first.startswith("the Sample Wiki: Wiki updated in"))
        self.assertTrue(second.startswith("the Sample Docs: Wiki updated in"))

    async def test_refresh_wiki_pings_nobody_a_site_names(self):
        # A failure names the address it couldn't reach, which the site wrote, so a mention in it must not ping.
        mention = f"<@{self.member_id}>"
        self.web.answer(SITEMAP, "<sitemapindex><sitemap><loc>https://wiki.example.org/" + mention.replace("<", "&lt;").replace(">", "&gt;") + "</loc></sitemap></sitemapindex>")
        self.dispatch_message(user_id=self.admin_id, content="!refresh_wiki")
        await self.drain()
        body = [body for method, path, body in self.net.calls if method == "POST" and path == "/channels/{channel_id}/messages"][-1]
        self.assertIn(mention, body["content"])
        self.assertEqual(body["allowed_mentions"]["parse"], [])
        self.assertNotIn("users", body["allowed_mentions"])

    async def test_refresh_wiki_while_one_runs(self):
        lock = self.answers.copies._locks.setdefault(self.source().key, asyncio.Lock())
        async with lock:
            await self.slash("refresh_wiki", user_id=self.admin_id)
        self.assertEqual(self.last_text(), "\N{CROSS MARK} " + pack.UPDATE_RUNNING)


class UpdatesTest(AnswersCase):
    async def test_the_timer_updates_the_copies_that_are_due_and_clears_old_rows(self):
        now = time.time()
        today = pack.utc_day()
        await self.db.run("INSERT INTO answer_uses (guild_id, user_id, day, uses) VALUES (?, 1, ?, 4), (?, 2, ?, 4)", (self.guild_id, today, self.guild_id, today - 3))
        await self.db.run("INSERT INTO answer_auto_days VALUES (?, ?, 9)", (self.guild_id, today - 5))
        await self.db.run("INSERT INTO answer_costs (guild_id, command, status, model, cost, at) VALUES (?, 'ask', 'ok', 'm', 1, ?), (?, 'ask', 'ok', 'm', 1, ?)",
                          (self.guild_id, now, self.guild_id, now - 80 * 86400))
        self.web.answer(SITEMAP, sitemap(BASE, {"/en/nozzle": "2026-03-01", "/en/plate": "2026-01-01"}))
        row = await self.bot.timers.get_key(pack.REFRESH_KIND, "all")
        self.assertIsNotNone(row, "the first look is booked at start")
        await self.answers._refresh_due({}, row)
        await self.drain()
        self.assertEqual(len(self.web.called(SITEMAP)), 1, "a copy made moments ago isn't due")
        self.assertGreater((await self.bot.timers.get_key(pack.REFRESH_KIND, "all"))["due_at"], now + pack.REFRESH_CHECK_SECONDS - 60)
        await self.answers.refresh_due_sources(now=now + 24 * 3600)  # exactly one day on, at any hour. 25 hours from 23:00 UTC lands two days on and clears today's row too
        self.assertEqual(len(self.web.called(BASE + "/en/nozzle")), 2, "a day later it is, and only the changed page comes down")
        self.assertEqual(len(self.web.called(BASE + "/en/plate")), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM answer_uses"), 1)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM answer_auto_days"), 0)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM answer_costs"), 1)

    async def test_a_copy_no_server_reads_goes(self):
        await self.configure(sources=[{**WIKI_ROW, "address": "https://other.example.org/sitemap.xml"}])
        await self.answers.refresh_due_sources()
        self.assertEqual(await self.message_db.scalar("SELECT COUNT(*) FROM wiki_copies WHERE source LIKE ?", ("%wiki.example.org%",)), 0)


class SpendTest(AnswersCase):
    async def test_this_month_and_last(self):
        now = time.time()
        this_month = pack._month_start(now)
        last_month = pack._month_start(now, 1)
        rows = [("ask", 0.10, this_month + 5), ("ask", 0.05, this_month + 6), ("how_do_i", 0.003, this_month + 7), ("ask", 2.0, last_month + 5), ("ask", 9.0, last_month - 5)]
        for command, cost, at in rows:
            await self.db.run("INSERT INTO answer_costs (guild_id, command, status, model, cost, at) VALUES (?, ?, 'ok', 'm', ?, ?)", (self.guild_id, command, cost, at))
        await self.db.run("INSERT INTO answer_costs (guild_id, command, status, model, cost, at) VALUES (?, 'ask', 'ok', 'm', 50, ?)", (self.guild_id + 1, this_month + 5))
        spend = await self.answers.spend(self.guild_id, now)
        self.assertEqual(spend["month"], {"cost": 0.153, "requests": 3, "by_command": [{"command": "ask", "requests": 2, "cost": 0.15}, {"command": "how_do_i", "requests": 1, "cost": 0.003}]})
        self.assertEqual(spend["last_month"], {"cost": 2.0, "requests": 1})

    def test_month_starts_cross_the_year(self):
        january = 1767225600 + 86400 * 3  # 2026-01-04 UTC
        self.assertEqual(pack._month_start(january), 1767225600)
        self.assertEqual(pack._month_start(january, 1), 1764547200)  # 2025-12-01


class DefaultsTest(unittest.TestCase):
    def test_a_new_server_starts_with_the_old_limits_models_and_efforts(self):
        defaults = Answers.schema.defaults()
        self.assertFalse(defaults["enabled"])
        self.assertEqual((defaults["daily_limit"], defaults["auto_answer_daily_limit"], defaults["monthly_limit"]), (25, 200, 200))
        self.assertEqual(defaults["models"], {"how_do_i": "claude-haiku-5-5", "brainstorm": "claude-sonnet-5-5", "ask": "claude-sonnet-5-5", "make_model": "claude-sonnet-5-5"})
        self.assertEqual(defaults["efforts"], {"how_do_i": "high", "brainstorm": "high", "ask": "high", "make_model": "medium"})
        self.assertEqual((pack.ROUNDS, pack.BURST_COUNT, pack.BURST_SECONDS, pack.CONCURRENCY), ({"how_do_i": 8, "brainstorm": 6, "ask": 8, "make_model": 8}, 3, 60.0, 3))


class CardTest(unittest.TestCase):
    def test_long_titles_many_sources_and_a_long_answer_fit_one_card(self):
        sources = [(f"Page [{n}] " + "long title " * 10, f"https://example.com/{n}") for n in range(40)]
        card = pack.answer_card("A question " * 40, pack.engine.Answer(status="ok", text="word " * 2000, sources=sources), "A footer").to_dict()
        self.assertLessEqual(len(card["title"]), 256)
        self.assertGreater(len(card["title"]), 250)
        self.assertTrue(card["title"].endswith("…"))
        self.assertEqual([field["name"] for field in card["fields"]], ["Sources", "More sources"])
        self.assertTrue(all(len(field["value"]) <= 1024 for field in card["fields"]))
        first = card["fields"][0]["value"].splitlines()[0]
        label = first[len("[1] ["):first.index("](")]
        self.assertEqual((len(label), label[:9], label[-1]), (60, "Page (0) ", "…"), "titles are cut to 60 characters, with brackets turned round")
        self.assertTrue(first.endswith("](https://example.com/0)"))
        total = len(card["title"]) + len(card["description"]) + len(card["footer"]["text"]) + sum(len(field["name"]) + len(field["value"]) for field in card["fields"])
        self.assertLessEqual(total, 6000)
        self.assertGreater(len(card["description"]), 2000, "the answer gets what the rest leaves")
        self.assertTrue(card["description"].endswith("…"))

    def test_a_question_at_a_line_break(self):
        self.assertEqual(pack.fit_text("one two\nthree four five six", 20), "one two\nthree four…")
        self.assertEqual(pack.fit_text("short", 20), "short")


class DashboardTest(BotTest):
    """The Wiki answers page's own routes, and who may change what the answers cost."""
    modules = [Answers]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.bot._connection.user = self.me
        self.manager = FakeMember(self.guild, "manager", self.guild.role("Manager", 4, "manage_guild", "view_channel"))
        self.cog = self.bot.module("answers")
        self.web = FakeOutside()
        self.cog.outside = self.web
        for name, value in (("FETCH_PAUSE", 0), ("RETRY_WAIT", 0)):
            patcher = mock.patch.object(wiki, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.web.answer(SITEMAP, sitemap(BASE, {"/en/nozzle": "2026-01-01"}))
        self.web.answer(BASE + "/en/nozzle", NOZZLE)
        servers.use({self.guild.id})
        self.addCleanup(servers.use, set())

    async def asyncTearDown(self):
        await self.cog.cog_unload()
        await super().asyncTearDown()

    async def site(self, member):
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(member.id, member.name, None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    async def call(self, member, method, tail, body=None):
        client = await self.site(member)
        response = await client.request(method, self.path(tail), json=body)
        return response.status, await response.json()

    async def settle_tasks(self):
        for task in list(self.cog._tasks):
            await asyncio.wait([task], timeout=10)

    async def test_only_an_administrator_changes_what_the_answers_cost(self):
        for form in ({"enabled": True}, {"daily_limit": 50}, {"models": {"ask": "claude-opus-5-5"}}, {"sources": [WIKI_ROW]}, {"rules": "Be brief."}):
            status, body = await self.call(self.manager, "PUT", "/settings/answers", form)
            self.assertEqual(status, 400, form)
            self.assertEqual(body["fields"], {next(iter(form)): access.ONLY_ADMINS}, form)
        self.assertEqual(self.settings("answers"), Answers.schema.defaults())
        status, _ = await self.call(self.admin, "PUT", "/settings/answers", {"enabled": True, "daily_limit": 50, "sources": [WIKI_ROW]})
        self.assertEqual(status, 200)
        await self.settle_tasks()
        self.assertEqual(self.settings("answers")["daily_limit"], 50)
        status, _ = await self.call(self.manager, "PUT", "/settings/answers", {"daily_limit": 50})
        self.assertEqual(status, 200, "a form that changes nothing goes through")

    async def test_the_host_decides_which_servers_may_switch_it_on(self):
        servers.use(set())
        status, body = await self.call(self.owner, "PUT", "/settings/answers", {"enabled": True})
        self.assertEqual((status, body["fields"]), (400, {"enabled": pack.NOT_ALLOWED}), "not even the owner")
        status, body = await self.call(self.admin, "GET", "/answers/status")
        self.assertFalse(body["allowed"])
        servers.use({self.guild.id})
        status, _ = await self.call(self.admin, "PUT", "/settings/answers", {"enabled": True})
        self.assertEqual(status, 200)
        status, body = await self.call(self.admin, "GET", "/answers/status")
        self.assertTrue(body["allowed"])

    async def test_a_source_the_bot_cant_use_is_refused(self):
        for row, words in (({**WIKI_ROW, "prefix": "en/"}, "Start the path with a slash"), ({**WIKI_ROW, "selector": "div[["}, "a CSS selector the bot can use"),
                           ({**WIKI_ROW, "exclude": ["/en/a b"]}, "can't hold spaces"), ({**WIKI_ROW, "address": "ftp://wiki.example.org/x"}, "")):
            status, body = await self.call(self.admin, "PUT", "/settings/answers", {"sources": [row]})
            self.assertEqual(status, 400, row)
            self.assertIn(words, json.dumps(body))
        status, body = await self.call(self.admin, "PUT", "/settings/answers", {"sources": [WIKI_ROW, {**WIKI_ROW, "name": "the sample wiki"}]})
        self.assertEqual(status, 400)
        self.assertIn("Two sources are named", json.dumps(body))
        status, body = await self.call(self.admin, "PUT", "/settings/answers", {"sources": [WIKI_ROW, {**WIKI_ROW, "name": "Again"}]})
        self.assertEqual(status, 400)
        self.assertIn("reads the same pages as another source", json.dumps(body))

    async def test_a_model_has_to_be_one_the_bot_knows_the_price_of(self):
        status, body = await self.call(self.admin, "PUT", "/settings/answers", {"models": {"how_do_i": "claude-opus-9"}})
        self.assertEqual(status, 400)
        self.assertIn("models.how_do_i", body["fields"])
        status, body = await self.call(self.admin, "PUT", "/settings/answers", {"models": {"ask": "claude-haiku-5-5"}})
        self.assertEqual(status, 400, "Haiku 5.5 can't run /ask's web search")
        status, _ = await self.call(self.admin, "PUT", "/settings/answers", {"models": {"how_do_i": "claude-opus-5-5", "ask": "claude-opus-5"}})
        self.assertEqual(status, 200)
        status, body = await self.call(self.admin, "GET", "/answers/status")
        self.assertEqual(body["model_choices"]["ask"], ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5"])
        self.assertEqual(body["model_choices"]["how_do_i"], ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5"])

    async def test_at_most_five_sources(self):
        rows = [{**WIKI_ROW, "name": f"Wiki {number}", "address": f"https://wiki{number}.example.org/sitemap.xml"} for number in range(6)]
        status, body = await self.call(self.admin, "PUT", "/settings/answers", {"sources": rows})
        self.assertEqual((status, body["fields"]), (400, {"sources": "Can hold at most 5 entries."}))
        self.assertEqual(pack.MAX_SOURCES, 5)

    async def test_the_status_says_what_is_missing_and_what_was_spent(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            status, body = await self.call(self.manager, "GET", "/answers/status")
        self.assertEqual(status, 200)
        self.assertEqual((body["key_set"], body["sources"], body["is_admin"]), (False, [], False))
        self.assertEqual(body["screening"], {"mode": "automod", "load_failed": False, "lists_loaded": False, "word_rule": False})
        self.assertEqual(body["spend"]["month"], {"cost": 0, "requests": 0, "by_command": []})
        await self.bot.settings.save(self.guild.id, "answers", {"enabled": True, "sources": [WIKI_ROW]})
        await self.settle_tasks()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            status, body = await self.call(self.admin, "GET", "/answers/status")
        self.assertEqual((body["key_set"], body["is_admin"]), (True, True))
        [row] = body["sources"]
        self.assertEqual((row["name"], row["pages"], row["loaded"], row["running"]), ("the Sample Wiki", 1, True, False))

    async def test_the_page_hears_that_the_lists_failed_and_not_where_they_live(self):
        screening.use(None, "/srv/private/filter/term_matching.py isn't a file.")
        self.addCleanup(screening.use, None)
        status, body = await self.call(self.manager, "GET", "/answers/status")
        self.assertTrue(body["screening"]["load_failed"])
        self.assertNotIn("/srv/private", json.dumps(body))

    async def test_the_status_names_the_screen_this_server_gets(self):
        holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(holder.cleanup)
        module, configs = write_term_lists(Path(holder.name), {self.guild.id + 1: {"blacklists": {"House": ["x"]}, "whitelists": {}}})
        screening.use(screening.load(module, configs))
        self.addCleanup(screening.use, None)
        status, body = await self.call(self.admin, "GET", "/answers/status")
        self.assertEqual((body["screening"]["mode"], body["screening"]["lists_loaded"]), ("automod", True), "lists for other servers only")
        (configs / f"{self.guild.id}.json").write_text(json.dumps({"blacklists": {"House": ["x"]}, "whitelists": {}}), encoding="utf-8")
        status, body = await self.call(self.admin, "GET", "/answers/status")
        self.assertEqual(body["screening"]["mode"], "lists")

    async def test_an_update_from_the_page(self):
        status, body = await self.call(self.manager, "POST", "/answers/refresh", {})
        self.assertEqual((status, body["error"]), (403, access.ONLY_ADMINS))
        status, body = await self.call(self.admin, "POST", "/answers/refresh", {})
        self.assertEqual((status, body["error"]), (400, pack.NO_WIKI))
        await self.bot.settings.save(self.guild.id, "answers", {"enabled": True, "sources": [WIKI_ROW, DOCS_ROW]})
        await self.settle_tasks()
        status, _ = await self.call(self.admin, "POST", "/answers/refresh", {"source": 2})
        self.assertEqual(status, 404)
        docs_calls = len(self.web.called(DOCS))
        lock = self.cog.copies._locks.setdefault(wiki.Source.of(WIKI_ROW).key, asyncio.Lock())
        async with lock:
            status, body = await self.call(self.admin, "POST", "/answers/refresh", {"source": 0})
        self.assertEqual((status, body["error"]), (409, pack.UPDATE_RUNNING))
        status, body = await self.call(self.admin, "POST", "/answers/refresh", {"source": 0})
        self.assertEqual((status, body), (200, {"started": True}))
        await self.settle_tasks()
        self.assertEqual(len(self.web.called(SITEMAP)), 2)
        self.assertEqual(len(self.web.called(DOCS)), docs_calls, "only the source asked for")
        entry = (await self.bot.activity_log.recent(self.guild.id))[0]
        self.assertEqual((entry["action"], entry["detail"]), ("answers.refresh", "the Sample Wiki"))
