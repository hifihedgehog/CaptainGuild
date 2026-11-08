"""One request through the model, as the AI service sees it: the rounds, the tools, the sources under the answer, the reply words, the
service's errors, what it cost, and sketches."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
import unittest
from unittest import mock

import httpx2

from captainguild.core.answers import engine, prompts
from captainguild.core.answers.engine import Answer, converse
from captainguild.core.answers.wiki import CRAWL, Library, Shelf, Source, WikiIndex
from tests.answer_fakes import ScriptedModel

BASE = "https://wiki.example.org"
SOURCE = Source(BASE + "/sitemap.xml", prefix="/en/")
ROWS = [
    (f"{BASE}/en/nozzle", "Replacing the Nozzle", "## Before you start {#before}\nLet the hotend cool.\n## Steps {#steps}\nUnscrew the nozzle with the wrench."),
    (f"{BASE}/en/plate", "Cleaning the Plate", "Wash the plate with dish soap and warm water."),
]
INDEX = WikiIndex(ROWS, SOURCE)
LIBRARY = Library([Shelf("the Sample Wiki", INDEX)])
TOOLS = prompts.wiki_tools({**prompts.SUBJECT, "wiki": "the Sample Wiki"})
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><rect x="5" y="5" width="40" height="20" fill="none" stroke="black"/><text x="50" y="30">Tom &amp; Jerry</text></svg>'


class LoopCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model = ScriptedModel()

    async def ask(self, **options):
        settings = {"system": "Be brief.", "tools": TOOLS, "model": "claude-haiku-5-5", "effort": "high", "max_rounds": 8, **options}
        library = settings.pop("library", LIBRARY)
        return await converse(self.model.client(), library, "How do I change the nozzle?", **settings)


class RoundsTest(LoopCase):
    async def test_a_search_then_an_answer_with_numbered_sources(self):
        self.model.reply(self.model.tool("search_wiki", {"query": "nozzle"}, input_tokens=500, cache_write=200),
                         self.model.text("Let it cool. [S1] Then unscrew it. [S2]", input_tokens=40, cache_read=300, output_tokens=50))
        answer = await self.ask()
        self.assertEqual(answer.status, "ok")
        self.assertEqual(answer.text, "Let it cool. [1] Then unscrew it. [1]")
        self.assertEqual(answer.sources, [("Replacing the Nozzle", f"{BASE}/en/nozzle")])  # two sections of one page make one source, linking the page
        self.assertEqual((answer.rounds, answer.tool_calls), (2, 1))
        self.assertEqual(dict(answer.usage), {"input_tokens": 540, "output_tokens": 150, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300})
        first = self.model.requests[0]
        self.assertEqual((first["model"], first["max_tokens"], first["system"]), ("claude-haiku-5-5", engine.MAX_TOKENS, "Be brief."))
        self.assertEqual(first["output_config"], {"effort": "high"})
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        self.assertEqual(first["tool_choice"], {"type": "auto"})
        self.assertEqual([tool["name"] for tool in first["tools"]], ["search_wiki", "read_wiki_page"])
        self.assertTrue(all(tool["strict"] for tool in first["tools"]))
        result = self.model.tool_results(1)[0]
        self.assertTrue(result["content"].startswith("[S1] Replacing the Nozzle › "))
        self.assertIn("Replacing the Nozzle › Before you start\n" + BASE + "/en/nozzle#before\nLet the hotend cool.", result["content"])
        self.assertNotIn("is_error", result)
        self.assertEqual(self.model.requests[1]["messages"][1]["content"][0]["type"], "thinking", "the thinking block goes back as it came")

    async def test_one_section_cited_links_that_section(self):
        self.model.reply(self.model.tool("search_wiki", {"query": "soap plate"}), self.model.text("Use soap. [S1]"))
        answer = await self.ask()
        self.assertEqual(answer.sources, [("Cleaning the Plate", f"{BASE}/en/plate")])
        self.model.reply(self.model.tool("search_wiki", {"query": "unscrew wrench"}), self.model.text("Unscrew it. [S1] Done. [S7]"))
        answer = await self.ask()
        self.assertEqual(answer.text, "Unscrew it. [1] Done.")  # a tag the model wasn't given is dropped
        self.assertEqual(answer.sources, [("Replacing the Nozzle › Steps", f"{BASE}/en/nozzle#steps")])

    async def test_a_whole_page_read_and_a_bad_address(self):
        self.model.reply(self.model.tool("read_wiki_page", {"url": "/en/nozzle"}), self.model.tool("read_wiki_page", {"url": "/en/nowhere"}), self.model.text("Done. [S2]"))
        answer = await self.ask()
        whole = self.model.tool_results(1)[0]["content"]
        self.assertIn("[S1] Replacing the Nozzle › Before you start", whole)
        self.assertIn("[S2] Replacing the Nozzle › Steps", whole)
        missing = self.model.tool_results(2)[0]
        self.assertTrue(missing["is_error"])
        self.assertIn("isn't a page in the wiki copy", missing["content"])
        self.assertEqual(answer.sources, [("Replacing the Nozzle › Steps", f"{BASE}/en/nozzle#steps")])

    async def test_the_last_round_cant_use_tools(self):
        self.model.reply(self.model.tool("search_wiki", {"query": "nozzle"}), self.model.tool("search_wiki", {"query": "again"}))
        answer = await self.ask(max_rounds=2)
        self.assertEqual(self.model.requests[1]["tool_choice"], {"type": "none"})
        self.assertEqual(answer.status, "error")  # it still sent no words
        self.assertEqual(answer.rounds, 2)

    async def test_a_paused_turn_goes_back_and_keeps_its_words(self):
        self.model.reply(self.model.message([{"type": "text", "text": "First part. "}], "pause_turn"), self.model.text("Second part."))
        answer = await self.ask()
        self.assertEqual(answer.text, "First part. Second part.")
        self.assertEqual(self.model.requests[1]["messages"][1]["role"], "assistant")

    async def test_working_notes_before_a_tool_call_arent_the_reply(self):
        self.model.reply(self.model.tool("search_wiki", {"query": "nozzle"}, words="Let me look that up."), self.model.text("Unscrew it. [S2]"))
        self.assertEqual((await self.ask()).text, "Unscrew it. [1]")
        self.model.reply(self.model.message([{"type": "text", "text": "Checking the web first. "}], "pause_turn"),
                         self.model.tool("search_wiki", {"query": "nozzle"}), self.model.text("Unscrew it. [S2]"))
        self.assertEqual((await self.ask()).text, "Unscrew it. [1]", "words from before a paused turn that then used a tool were notes too")

    async def test_a_refusal_ends_the_request(self):
        self.model.reply(self.model.text("", stop_reason="refusal"))
        self.assertEqual((await self.ask()).status, "refused")

    async def test_running_out_of_tokens_without_words_is_too_long(self):
        self.model.reply(self.model.message([{"type": "thinking", "thinking": "", "signature": "s"}], "max_tokens"))
        self.assertEqual((await self.ask()).status, "too_long")
        cut_call = self.model.tool("search_wiki", {"query": "x"})
        cut_call["stop_reason"] = "max_tokens"
        self.model.reply(cut_call)
        self.assertEqual((await self.ask()).status, "too_long")
        self.model.reply(self.model.text("Enough words to send.", stop_reason="max_tokens"))
        self.assertEqual((await self.ask()).status, "ok")

    async def test_every_round_streams_with_room_for_thinking_at_max_effort(self):
        self.model.reply(self.model.tool("search_wiki", {"query": "nozzle"}), self.model.text("Unscrew it. [S2]"))
        answer = await self.ask(effort="max")
        self.assertEqual(answer.status, "ok")
        self.assertEqual([request.get("stream") for request in self.model.requests], [True, True])
        self.assertEqual({request["max_tokens"] for request in self.model.requests}, {64000})
        self.assertGreater(engine.MAX_TOKENS, 20000, "Opus 5.5 at Max effort thought past 20,000 tokens on a plain request")

    async def test_reads_wait_out_a_long_silent_thought(self):
        timeout = engine.client_timeout()
        self.assertEqual((timeout.read, timeout.connect), (1200.0, 10.0))
        self.assertGreater(timeout.read, engine.MAX_TOKENS / 90, "the thinking isn't shown, so the stream can stay quiet while all of it is written")

    async def test_an_error_partway_through_a_stream_reads_as_the_service_down(self):
        self.model.reply(self.model.broken("overloaded_error", "Overloaded"))
        with self.assertLogs(engine.log, level="WARNING"):
            self.assertEqual((await self.ask()).status, "unavailable")
        self.model.reply(self.model.broken("invalid_request_error", "Bad"))
        with self.assertLogs(engine.log, level="WARNING"):
            self.assertEqual((await self.ask()).status, "error")

    async def test_a_request_past_the_time_limit_stops(self):
        async def slow(client, **request):
            await asyncio.sleep(5)

        with mock.patch.object(engine, "ANSWER_SECONDS", 0.05), mock.patch.object(engine, "final_message", slow), self.assertLogs(engine.log, level="WARNING"):
            answer = await self.ask()
        self.assertEqual(answer.status, "too_slow")

    async def test_a_round_that_starts_late_has_to_answer(self):
        self.model.reply(self.model.text("Unscrew it. [S2]"))
        with mock.patch.object(engine, "LAST_ROUND_AFTER", 0):
            answer = await self.ask()
        self.assertEqual(self.model.requests[0]["tool_choice"], {"type": "none"})
        self.assertEqual(answer.status, "ok")


    async def test_with_two_sources_each_passage_names_its_own(self):
        docs = WikiIndex([("https://docs.example.org/nozzle-wear", "Nozzle wear", "A worn nozzle under-extrudes.")], Source("https://docs.example.org/", kind=CRAWL))
        library = Library([Shelf("the Sample Wiki", INDEX), Shelf("the Sample Docs", docs)])
        self.model.reply(self.model.tool("search_wiki", {"query": "worn nozzle wrench"}), self.model.text("It wears. [S3]"))
        answer = await self.ask(library=library)
        results = self.model.tool_results(1)[0]["content"]
        self.assertIn("[S1] Replacing the Nozzle › Steps (the Sample Wiki)\n" + BASE + "/en/nozzle#steps", results)
        self.assertIn("[S3] Nozzle wear (the Sample Docs)\nhttps://docs.example.org/nozzle-wear", results)
        self.assertEqual(answer.sources, [("Nozzle wear", "https://docs.example.org/nozzle-wear")], "the source's name stays out of the link's title")


class ReplyWordsTest(LoopCase):
    async def test_decline_ask_and_skip(self):
        cases = [
            ("DECLINE", {}, "declined"), ("Decline.", {"auto": True}, "skipped"), ("ASK", {"redirect": True}, "redirect"), ("ASK", {"redirect": True, "auto": True}, "skipped"),
            ("ASK", {}, "ok"), ("SKIP", {"auto": True}, "skipped"), ("SKIP", {}, "ok"), ("Ask the maker's support about a refund, they handle orders.", {"redirect": True}, "ok"),
        ]
        for words, options, status in cases:
            self.model.reply(self.model.text(words))
            self.assertEqual((await self.ask(**options)).status, status, (words, options))


class WebTest(LoopCase):
    def searched(self, content):
        return self.model.message([
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "price"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": "https://shop.example.com/printer/", "title": "Printer at the shop", "encrypted_content": "x", "page_age": None}]},
            *content], "end_turn", web_searches=1)

    async def test_web_tags_name_only_pages_the_search_returned(self):
        self.model.reply(self.searched([{"type": "text", "text": "It costs $500. [W: https://shop.example.com/printer] Cheaper elsewhere. [W: https://made.up/page]"}]))
        answer = await self.ask()
        self.assertEqual(answer.text, "It costs $500. [1] Cheaper elsewhere.")
        self.assertEqual(answer.sources, [("Printer at the shop", "https://shop.example.com/printer/")])
        self.assertEqual(answer.usage["web_search_requests"], 1)

    async def test_a_web_citation_becomes_a_source(self):
        cited = {"type": "text", "text": "It costs $500.", "citations": [{"type": "web_search_result_location", "url": "https://shop.example.com/printer/", "title": "Printer at the shop",
                                                                        "cited_text": "$500", "encrypted_index": "x"}]}
        self.model.reply(self.searched([cited]))
        answer = await self.ask()
        self.assertEqual(answer.text, "It costs $500. [1]")
        self.assertEqual(answer.sources, [("Printer at the shop", "https://shop.example.com/printer/")])

    def test_the_web_search_tool_leaves_out_the_sources_sites(self):
        tool = prompts.web_search_tool([BASE + "/sitemap.xml", "https://docs.example.org/start", BASE + "/other.xml"])
        self.assertEqual(tool, {"type": "web_search_20260209", "name": "web_search", "max_uses": 5, "blocked_domains": ["wiki.example.org", "docs.example.org"]})
        self.assertNotIn("blocked_domains", prompts.web_search_tool([]))


def calls(*pairs: tuple[str, dict]) -> dict:
    """One reply asking for several tools at once."""
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    blocks += [{"type": "tool_use", "id": f"toolu_many_{number}", "name": name, "input": args} for number, (name, args) in enumerate(pairs)]
    return ScriptedModel.message(blocks, "tool_use")


class ToolCapsTest(LoopCase):
    """One answer can't run tools without end: a cap on calls, on each costly tool, and on how much of the wiki it reads."""

    async def test_calls_past_the_cap_get_an_error_instead_of_running(self):
        searches = [("search_wiki", {"query": f"nozzle {number}"}) for number in range(engine.MAX_TOOL_CALLS + 3)]
        self.model.reply(calls(*searches), self.model.text("Done. [S1]"))
        answer = await self.ask()
        results = self.model.tool_results(1)
        self.assertEqual(len(results), engine.MAX_TOOL_CALLS + 3)
        self.assertFalse(any(result.get("is_error") for result in results[:engine.MAX_TOOL_CALLS]))
        self.assertTrue(all(result.get("is_error") for result in results[engine.MAX_TOOL_CALLS:]))
        self.assertEqual(results[-1]["content"], engine.TOO_MANY_CALLS)
        self.assertEqual(answer.status, "ok")
        self.assertEqual(engine.MAX_TOOL_CALLS, 40)

    async def test_a_costly_tool_runs_only_as_often_as_its_limit(self):
        ran = []

        async def render(args, answer):
            ran.append(args)
            return "Rendered.", False
        self.model.reply(calls(*[("render_model", {"name": "x", "scad": "cube(1);"})] * 6), self.model.text("Done."))
        await self.ask(handlers={"render_model": render}, tool_limits={"render_model": 4})
        results = self.model.tool_results(1)
        self.assertEqual(len(ran), 4)
        self.assertEqual([result.get("is_error", False) for result in results], [False] * 4 + [True] * 2)
        self.assertEqual(results[-1]["content"], engine.CALLS_USED.format(tool="render_model"))

    async def test_reading_stops_at_the_budget(self):
        with mock.patch.object(engine, "READ_BUDGET", 150):
            self.model.reply(calls(("read_wiki_page", {"url": "/en/nozzle"}), ("read_wiki_page", {"url": "/en/plate"})), self.model.text("Done. [S1]"))
            await self.ask()
        first, second = self.model.tool_results(1)
        self.assertFalse(first.get("is_error", False))
        self.assertTrue(second["is_error"])
        self.assertEqual(second["content"], engine.READ_ALL)


class ServiceErrorsTest(LoopCase):
    async def status_for(self, reply):
        self.model.reply(reply)
        with self.assertLogs("captainguild.core.answers.engine", level="DEBUG") as caught:
            logging.getLogger("captainguild.core.answers.engine").debug("start")
            answer = await self.ask()
        return answer.status, caught.output

    async def test_each_kind_of_failure_has_its_status(self):
        cases = [
            (self.model.error(429, "rate_limit_error", "slow down"), "busy"),
            (self.model.error(401, "authentication_error", "invalid x-api-key"), "key_refused"),
            (self.model.error(403, "permission_error", "no"), "key_refused"),
            (self.model.error(500, "api_error", "oops"), "unavailable"),
            (self.model.error(529, "overloaded_error", "busy"), "unavailable"),
            (self.model.error(400, "invalid_request_error", "Your credit balance is too low to access the API."), "unavailable"),
            (self.model.error(400, "invalid_request_error", "messages: bad"), "error"),
            (httpx2.ConnectError("no route"), "unavailable"),
        ]
        for reply, status in cases:
            self.assertEqual((await self.status_for(reply))[0], status, reply)

    async def test_a_fault_here_is_logged_and_answers_error(self):
        async def broken(args, answer):
            raise RuntimeError("bug")
        self.model.reply(self.model.tool("draw_sketch", {"svg": "<svg/>", "caption": "x"}))
        with self.assertLogs("captainguild.core.answers.engine", level="ERROR") as caught:
            answer = await self.ask(handlers={"draw_sketch": broken})
        self.assertEqual(answer.status, "error")
        self.assertIn("RuntimeError: bug", caught.output[0])


class CostTest(unittest.TestCase):
    MILLION = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}

    def test_the_price_list(self):
        # List prices per million tokens: input, output, cache write, cache read.
        expected = {"claude-haiku-5-5": 0.10 + 0.50 + 0.125 + 0.01, "claude-sonnet-5-5": 2.00 + 10.00 + 2.50 + 0.20,
                    "claude-opus-5-5": 4.00 + 20.00 + 5.00 + 0.20, "claude-opus-5": 5.00 + 25.00 + 6.25 + 0.50}
        self.assertEqual(set(engine.PRICES), set(expected))
        small = {"input_tokens": 50_000, "output_tokens": 1_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        for model, total in expected.items():
            self.assertAlmostEqual(engine.token_cost(model, {key: value // 100 for key, value in self.MILLION.items()}) * 100, total, msg=model)
            self.assertGreater(engine.token_cost(model, small), 0, model)
        self.assertAlmostEqual(engine.token_cost("some-new-model", small), engine.token_cost("claude-opus-5", small), msg="a model missing from the list costs as much as the dearest one")

    def test_a_long_haiku_prompt_bills_at_the_long_rate(self):
        round_ = {"input_tokens": 20_000, "output_tokens": 1_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 100_000}
        self.assertAlmostEqual(engine.token_cost("claude-haiku-5-5", round_), (20_000 * 0.50 + 1_000 * 2.50 + 100_000 * 0.05) / 1_000_000)
        short = {**round_, "cache_read_input_tokens": 80_000}
        self.assertAlmostEqual(engine.token_cost("claude-haiku-5-5", short), (20_000 * 0.10 + 1_000 * 0.50 + 80_000 * 0.01) / 1_000_000)

    def test_an_answer_costs_its_rounds_and_its_searches(self):
        answer = Answer(status="ok", model="claude-haiku-5-5")
        answer.billed = 0.25
        answer.usage["web_search_requests"] = 3
        self.assertAlmostEqual(answer.cost, 0.25 + 0.03)


class SketchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not engine.sketches_available():
            self.skipTest("The cairo library isn't on this machine.")

    async def test_one_sketch_renders_and_its_words_go_to_the_screen(self):
        answer = Answer(status="ok")
        words, failed = await engine.draw_sketch({"svg": SVG, "caption": "c" * 300}, answer)
        self.assertFalse(failed, words)
        self.assertTrue(answer.image.startswith(b"\x89PNG"))
        self.assertEqual(int.from_bytes(answer.image[16:20], "big"), 1200, "drawn 1,200 pixels wide")
        self.assertEqual(len(answer.image_caption), 200)
        self.assertEqual(answer.screen_text, ["Tom & Jerry"])
        again, failed = await engine.draw_sketch({"svg": SVG, "caption": "x"}, answer)
        self.assertTrue(failed)
        self.assertIn("already attached", again)

    async def test_a_run_of_text_reaches_the_screen_joined_as_cairosvg_draws_it(self):
        # cairosvg 2.9.1 draws a <text>, <textPath> or <a> and everything inside it as one run (cairosvg/parser.py:224-226, 246-311).
        # Inside it, tspan, textPath, a and text draw their own text (surface.py:50-72), tref draws the text of the element it names
        # (parser.py:262-280), any other element draws only what follows its closing tag (293-306), and a line break is dropped unless
        # xml:space is preserve (handle_white_spaces, 57-70). Each of the joined cases draws the same picture as "badword". Separate
        # <text> elements stay apart.
        head = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
        cases = [
            ("<text x='5' y='20'>bad<tspan>word</tspan></text>", ["bad word", "badword"]),
            ("<text x='5' y='20'>bad<tspan>wo<tspan>r</tspan></tspan>d</text>", ["bad wo r d", "badword"]),
            ("<text x='5' y='20'>b<a href='#x'>ad</a>word</text>", ["b ad word", "badword"]),
            ("<text x='5' y='20'>bad\nword</text>", ["bad word", "badword"]),
            ("<text x='5' y='20'>bad<title>X</title>word</text>", ["bad X word", "badword"]),
            ("<text x='5' y='20'>bad<g>X<tspan>wor</tspan></g>d</text>", ["bad X wor d", "badword"]),
            ("<defs><text id='w'>wo<tspan>rd</tspan></text></defs><text x='5' y='20'>bad<tref href='#w'/></text>",
             ["wo rd bad", "word badword"]),
            ("<defs><path id='p' d='M5 20 H95'/></defs><text x='5' y='20'>bad<textPath href='#p'>word</textPath></text>",
             ["bad word", "badword"]),
            ("<text x='5' y='20' xml:space='preserve'>bad\nword</text>", ["bad word"]),
            ("<text x='5' y='20'>bad</text><text x='5' y='40'>word</text>", ["bad word"]),
        ]
        for body, screened in cases:
            with self.subTest(body=body):
                answer = Answer(status="ok")
                words, failed = await engine.draw_sketch({"svg": head + body + "</svg>", "caption": "x"}, answer)
                self.assertFalse(failed, words)
                self.assertEqual(answer.screen_text, screened)

    async def test_character_references_and_entities_reach_the_screen_decoded(self):
        # cairosvg reads the SVG as XML, which turns &#97; and &#x61; into the letter and decodes the five predefined entities, so the
        # screen gets the words the way they're drawn.
        svg = SVG.replace("Tom &amp; Jerry", "b&#97;dword b&#x61;dword &amp; &lt; &gt; &quot; &apos;")
        answer = Answer(status="ok")
        words, failed = await engine.draw_sketch({"svg": svg, "caption": "x"}, answer)
        self.assertFalse(failed, words)
        self.assertEqual(answer.screen_text, ["badword badword & < > " + '"' + " '"])

    async def test_svg_that_could_reach_outside_is_refused(self):
        head = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        refused = "The SVG uses images, reused groups, outside links, scripts, comments, or declarations."
        cases = [
            (head + '<image href="#local" width="5" height="5"/></svg>', refused),
            (head + '<a href="https://x.example/"><rect width="5" height="5"/></a></svg>', refused),
            (head + "<script>x</script></svg>", refused),
            ('<!DOCTYPE svg [<!ENTITY a "b">]>' + head + "</svg>", refused),
            # cairosvg draws text from a CDATA section, and a comment between letters vanishes from the drawing, yet both hide the words
            # from the screen, which reads the text between tags.
            (head + '<text x="1" y="5"><![CDATA[badword]]></text></svg>', refused),
            (head + '<text x="1" y="5">bad<!-- -->word</text></svg>', refused),
            # Nested reuse of one group draws it many thousands of times from a few kilobytes.
            (head + '<defs><g id="g"><rect width="1" height="1"/></g></defs><use href="#g"/></svg>', refused),
            (head + '<rect width="5" height="5" style="fill:url(https://x.example/a)"/></svg>', refused),
            (head + "<style>@import 'x.css';</style></svg>", refused),
            ("not svg at all", "That isn't an SVG document."),
            (head + "x" * 120_001 + "</svg>", "over 120000 characters"),
        ]
        for svg, words in cases:
            answer = Answer(status="ok")
            said, failed = await engine.draw_sketch({"svg": svg, "caption": "x"}, answer)
            self.assertTrue(failed, svg[:60])
            self.assertIn(words, said, svg[:60])
            self.assertIsNone(answer.image)
        inside = head + '<a href="#top"><rect fill="url(#g)" width="5" height="5"/></a></svg>'
        self.assertFalse((await engine.draw_sketch({"svg": inside, "caption": ""}, Answer(status="ok")))[1], "a link inside the drawing is allowed")

    async def test_a_drawing_that_runs_long_is_stopped(self):
        started = time.monotonic()
        with mock.patch.object(engine, "SKETCH_SECONDS", 0.05):  # less than the renderer takes to start, so the limit always lands
            words, failed = await engine.draw_sketch({"svg": SVG, "caption": "x"}, Answer(status="ok"))
        self.assertTrue(failed)
        self.assertIn("The sketch took over 0.05 seconds to draw.", words)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(engine.SKETCH_SECONDS, 20)

    async def test_the_renderer_runs_in_its_own_process(self):
        started = []
        real = subprocess.Popen

        def watched(command, *args, **kwargs):
            started.append(command)
            return real(command, *args, **kwargs)
        with mock.patch.object(engine.subprocess, "Popen", watched):
            words, failed = await engine.draw_sketch({"svg": SVG, "caption": "x"}, Answer(status="ok"))
            self.assertFalse(failed, words)
            words, failed = await engine.draw_sketch({"svg": "<svg><rect", "caption": "x"}, Answer(status="ok"))
        self.assertTrue(failed)
        self.assertIn("The sketch didn't render:", words)
        self.assertNotIn("Error:", words, "the renderer's own words, without the name of its exception")
        self.assertEqual(len(started), 2)
        self.assertIn(sys.executable, started[0])
        self.assertIn("cairosvg", " ".join(started[0]))

    async def test_a_machine_without_cairo_says_so(self):
        with mock.patch.object(engine, "_cairo_loaded", False):
            words, failed = await engine.draw_sketch({"svg": SVG, "caption": "x"}, Answer(status="ok"))
        self.assertTrue(failed)
        self.assertEqual(words, "Sketches aren't available on this bot. Write the plan without one.")


if __name__ == "__main__":
    unittest.main()
