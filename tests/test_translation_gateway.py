"""The translation pack through the real discord.py library: answers to messages, flag reactions, the Translate menu, the emoji export, and
what the pack's switch and settings change."""
from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from unittest import mock

import aiohttp
import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core import translator as tr
from captainguild.modules import load_all
from captainguild.modules import translation as translation_module
from captainguild.modules.mydata import BY_KEY
from captainguild.modules.translation import DELETE_KIND, Translation
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest
from tests.gateway import GatewayTest, new_id, user_json
from tests.test_translation import NAMES, RATES, deepl, rates_site

SPAIN, USA, BRITAIN = "\U0001f1ea\U0001f1f8", "\U0001f1fa\U0001f1f8", "\U0001f1ec\U0001f1e7"
REPLY = "POST /channels/{channel_id}/messages"


class PackCase(GatewayTest):
    modules = [Translation]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pack = self.bot.module("translation")
        self.deepl_calls: list = []
        self.web = rates_site(FakeOutside())
        self.web.answer(tr.FREE_URL, deepl(calls=self.deepl_calls))
        self.pack.outside = self.web
        self.pack.translator.pace = 0
        await self.switch(enabled=True)

    async def switch(self, **form) -> None:
        await self.bot.settings.save(self.guild_id, "translation", form)

    def bot_posts(self) -> list[dict]:
        return [body for method, path, body in self.net.calls if f"{method} {path}" == REPLY]

    async def say(self, content: str, *, user_id: int | None = None, channel: dict | None = None, **extra) -> dict:
        payload = self.dispatch_message(user_id=user_id or self.member_id, content=content, channel=channel, **extra)
        self.net.messages[int(payload["id"])] = payload
        await self.settle()
        return payload


class SentenceTest(PackCase):
    async def test_a_currency_question_in_words(self):
        asked = await self.say("how much is 20 bucks in euros?")
        posts = self.bot_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["content"], "20.00 bucks (USD - US Dollar) ≈ **18.00** euros (EUR - Euro) (as of 2026-11-06)\n(1 bucks (USD - US Dollar) ≈ 0.9 euros (EUR - Euro))")
        self.assertEqual(posts[0]["message_reference"]["message_id"], int(asked["id"]))
        self.assertEqual(posts[0]["allowed_mentions"], {"parse": [], "replied_user": False})
        self.assert_no_errors()

    async def test_moose_pesos_and_freedom_farthings(self):
        await self.say("20 moose pesos to freedom farthings")
        self.assertIn("\U0001face \U0001f985\U0001f1fa\U0001f1f8 (as of", self.bot_posts()[0]["content"])

    async def test_a_coin_whose_name_holds_in(self):
        await self.say("1 bitcoin to usd")
        self.assertTrue(self.bot_posts()[0]["content"].startswith("1.00 bitcoin (BTC - Bitcoin) ≈ **100,000.00** USD - US Dollar (as of 2026-11-06)"), self.bot_posts()[0]["content"])

    async def test_bob_is_the_bolivian_boliviano(self):
        rates_site(self.web, {**RATES, "usd": {**RATES["usd"], "bob": 6.9}}, {**NAMES, "bob": "Bolivian Boliviano"})
        await self.say("100 bob to usd")
        self.assertTrue(self.bot_posts()[0]["content"].startswith("100.00 BOB - Bolivian Boliviano ≈ **14.49** USD - US Dollar"), self.bot_posts()[0]["content"])

    async def test_two_codes_get_the_short_answer(self):
        await self.say("100 usd eur please")
        self.assertEqual(self.bot_posts()[0]["content"], "100.00 USD - US Dollar ≈ **90.00** EUR - Euro (as of 2026-11-06)")

    async def test_a_currency_name_cant_add_formatting(self):
        rates_site(self.web, {**RATES, "usd": {**RATES["usd"], "abc": 2}}, {**NAMES, "abc": "Under_score *star*"})
        await self.say("100 usd abc")
        self.assertIn(r"ABC - Under\_score \*star\*", self.bot_posts()[0]["content"])

    async def test_units_in_words(self):
        await self.say("convert 5 km to miles")
        self.assertEqual(self.bot_posts()[0]["content"], "5.00 km ≈ **3.1068559612** miles")

    async def test_fractions_decimal_commas_and_ranges(self):
        for text, expected in (
            ("3/4 inch to mm", "0.75 inch ≈ **19.0500000000** mm"), ("1,75 mm to inches", "1.75 mm ≈ **0.0688976378** inches"),
            ("190-220 C in F", "220.00 C ≈ **428.0000000000** F"),
            ("1/2 btc to usd", "0.50 BTC - Bitcoin ≈ **50,000.00** USD - US Dollar"), ("1,5 btc to usd", "1.50 BTC - Bitcoin ≈ **150,000.00** USD - US Dollar"),
            ("10-20 usd to eur", "20.00 USD - US Dollar ≈ **18.00** EUR - Euro"),
        ):
            before = len(self.bot_posts())
            await self.say(text)
            self.assertTrue(self.bot_posts()[before]["content"].startswith(expected), (text, self.bot_posts()[before]["content"]))

    async def test_mixed_numbers_in_words(self):
        # Inches are written as mixed numbers, and "1 1/2 inch to mm" got no answer.
        for text, expected in (("1 1/2 inch to mm", "1.50 inch ≈ **38.1000000000** mm"), ("2-3/4 in to cm", "2.75 in ≈ **6.9850000000** cm"),
                               ("1 1/2 btc to usd", "1.50 BTC - Bitcoin ≈ **150,000.00** USD - US Dollar")):
            before = len(self.bot_posts())
            await self.say(text)
            self.assertTrue(self.bot_posts()[before]["content"].startswith(expected), (text, self.bot_posts()[before]["content"]))

    async def test_unit_marks_and_case_in_words(self):
        for text, expected in (("200 \u00b5m to mm", "200.00 \u00b5m ≈ **0.2000000000** mm"), ("5 m\u00b2 to ft\u00b2", "5.00 m\u00b2 ≈ **53.8195520835** ft\u00b2"),
                               ("500 mW to W", "500.00 mW ≈ **0.5000000000** W"), ("100 Mb to MB", "100.00 Mb ≈ **12.5000000000** MB")):
            before = len(self.bot_posts())
            await self.say(text)
            self.assertEqual(self.bot_posts()[before]["content"], expected, text)

    async def test_chat_with_numbers_gets_no_answer(self):
        for text in ("I have 2 kids in school", "it took 3 hours to print", "100 usd", "I need 3 cables for 5 printers", "5 lol lol"):
            await self.say(text)
        self.assertEqual(self.bot_posts(), [])

    async def test_chat_naming_one_currency_or_unit_twice_gets_no_answer(self):
        # ALL is the Albanian lek's code, so "9 all in all" read as 9 lek in lek, and a range read as 200 C into a unit called 210 C.
        rates_site(self.web, {**RATES, "usd": {**RATES["usd"], "all": 92.0}}, {**NAMES, "all": "Albanian Lek"})
        for text in ("I'd rate it a 9 all in all", "we spent 40 all in all on filament", "raise the nozzle from 200 C to 210 C",
                     "I changed the speed from 200 mm/s to 300 mm/s", "that's 5 km to km", "10 usd in usd", "10 USD USD"):
            await self.say(text)
        self.assertEqual(self.bot_posts(), [])
        await self.say("10 usd to all")
        self.assertEqual(len(self.bot_posts()), 1, "a real conversion into lek still answers")

    async def test_a_command_isnt_also_read_as_a_question(self):
        await self.say("!currency 100 usd eur", user_id=self.owner_id)
        posts = self.bot_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("100.00 USD - US Dollar = **90.00** EUR - Euro", posts[0]["content"])

    async def test_another_bot_gets_conversions_but_not_translations(self):
        other = user_json(new_id(), "otherbot", bot=True)
        for content in ("100 usd to eur", "translate hola to english"):
            payload = {**self.net.message_json(int(self.text["id"]), {"content": content}), "author": other}
            self.bot._connection.parse_message_create(payload)
            await self.settle()
        self.assertEqual(len(self.bot_posts()), 1)
        self.assertEqual(self.deepl_calls, [])

    async def test_the_bots_own_posts_and_its_webhooks_get_nothing(self):
        mine = self.net.message_json(int(self.text["id"]), {"content": "100 usd to eur"})
        self.bot._connection.parse_message_create(mine)
        hook_id = new_id()
        posted = {**self.net.message_json(int(self.text["id"]), {"content": "5 km to mi"}, webhook_id=hook_id),
                  "author": user_json(hook_id, "A member's name", bot=True), "application_id": str(self.bot_id)}
        self.bot._connection.parse_message_create(posted)
        await self.settle()
        self.assertEqual(self.bot_posts(), [])
        other_hook = {**posted, "id": str(new_id()), "application_id": None}
        self.bot._connection.parse_message_create(other_hook)
        await self.settle()
        self.assertEqual(len(self.bot_posts()), 1, "another webhook's post is answered, as before")

    async def test_each_switch_and_the_channels_left_out(self):
        await self.switch(answer_units=False)
        await self.say("5 km to mi")
        self.assertEqual(self.bot_posts(), [])
        await self.say("100 usd to eur")
        self.assertEqual(len(self.bot_posts()), 1)
        await self.switch(answer_units=True, answer_currency=False)
        await self.say("100 usd to eur")
        self.assertEqual(len(self.bot_posts()), 1)
        await self.switch(answer_currency=True, ignored_channels=[int(self.category["id"])])
        await self.say("100 usd to eur")
        await self.say("5 km to mi")
        self.assertEqual(len(self.bot_posts()), 1, "a category left out covers its channels")

    async def test_a_pack_switched_off_answers_nothing(self):
        await self.switch(enabled=False)
        await self.say("100 usd to eur")
        await self.say("translate hola to english")
        self.assertEqual(self.bot_posts(), [])

    async def test_without_message_content_it_stands_down(self):
        self.bot.reads_message_text = False
        await self.say("100 usd to eur")
        self.assertEqual(self.bot_posts(), [])
        self.assertEqual(self.web.calls, [])


class CommandAmountTest(PackCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.sync_server_commands(self.guild_id)

    async def run_command(self, name: str, **options: str) -> str:
        """Runs a slash command with text options and gives the text of its last answer, sent at once or after a wait."""
        mark = len(self.net.calls)
        data = {"id": str(new_id()), "name": name, "type": 1, "guild_id": str(self.guild_id),
                "options": [{"name": key, "type": 3, "value": value} for key, value in options.items()]}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.member_id))
        await self.settle()
        self.assert_no_errors()
        sent = [body for method, path, body in self.net.calls[mark:] if method == "POST" and ("webhook" in path or path.endswith("/callback"))]
        return sent[-1].get("data", sent[-1])["content"]

    async def test_the_commands_take_an_amount_as_written(self):
        # The amount was a number field, which can't hold 1 1/2 or 1,75.
        for command in (self.pack.units, self.pack.currency):
            amount = next(parameter for parameter in command.app_command.parameters if parameter.name == "amount")
            self.assertEqual(amount.type, discord.AppCommandOptionType.string, command.name)
        self.assertEqual(await self.run_command("units", amount="1 1/2", from_unit="inch", to_unit="mm"), "1.50 inch = **38.1000000000** mm")
        self.assertEqual(await self.run_command("units", amount="1,75", from_unit="mm", to_unit="in"), "1.75 mm = **0.0688976378** in")
        said = await self.run_command("currency", amount="1-1/2", from_currency="btc", to_currency="usd")
        self.assertTrue(said.startswith("1.50 BTC - Bitcoin = **150,000.00** USD - US Dollar"), said)
        self.assertEqual(await self.run_command("units", amount="lots", from_unit="mm", to_unit="in"),
                         "\N{CROSS MARK} `lots` isn't an amount. Write it as 5, 2.5, 3/4 or 1 1/2.")

    async def test_typed_after_a_prefix_a_mixed_number_takes_a_hyphen_or_quotes(self):
        # A typed command splits its words at spaces.
        for text in ("!units 1-1/2 inch mm", '!units "1 1/2" inch mm'):
            before = len(self.bot_posts())
            await self.say(text)
            self.assertEqual(self.bot_posts()[before]["content"], "1.50 inch = **38.1000000000** mm", text)


class TranslateMessageTest(PackCase):
    async def test_translate_with_a_language(self):
        await self.say("translate hola amigo to english")
        post = self.bot_posts()[0]
        self.assertEqual(post["content"], "\U0001f310 **Spanish → English (US)**\n[en-US] hola amigo")
        self.assertEqual(post["allowed_mentions"]["parse"], [])
        self.assertEqual(self.deepl_calls[0]["target_lang"], "en-US")

    async def test_with_no_language_it_goes_to_the_members_own(self):
        await self.pack.db.run("INSERT INTO translate_languages (guild_id, user_id, language, updated_at) VALUES (?, ?, 'DE', 1)", (self.guild_id, self.member_id))
        await self.say("translate good morning")
        self.assertEqual(self.deepl_calls[0]["target_lang"], "de")

    async def test_a_reply_translates_the_message_it_answers(self):
        original = await self.say("bonjour tout le monde", user_id=self.mod_id)
        await self.say("translate to spanish", message_reference={"message_id": original["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)},
                       referenced_message=original, type=19)
        self.assertEqual(self.deepl_calls[0]["text"], ["bonjour tout le monde"])
        self.assertEqual(self.deepl_calls[0]["target_lang"], "es")

    async def test_a_reply_whose_message_isnt_sent_along_is_fetched(self):
        original = await self.say("guten tag", user_id=self.mod_id)
        await self.say("translate", message_reference={"message_id": original["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)}, type=19)
        self.assertEqual(self.deepl_calls[0]["text"], ["guten tag"])
        self.assertEqual(self.deepl_calls[0]["target_lang"], "en-US")

    async def test_nothing_to_translate_gets_the_hint(self):
        await self.say("translate to french")
        self.assertEqual(self.bot_posts()[0]["content"], "\U0001f310 Give me something to translate: reply to a message, or type `translate hola to english`.")
        await self.say("translates are fun")
        self.assertEqual(len(self.bot_posts()), 1)

    async def test_the_sixth_in_thirty_seconds_gets_no_answer(self):
        for _ in range(6):
            await self.say("translate hola to english")
        self.assertEqual(len(self.bot_posts()), 5)

    async def test_a_service_error_is_said(self):
        self.web.answer(tr.FREE_URL, reply({"message": "Bad"}, 503))
        await self.say("translate hola to english")
        self.assertEqual(self.bot_posts()[0]["content"], "\U0001f310 The translation service had a problem. Try again shortly.")

    async def test_the_switch(self):
        await self.switch(answer_translate=False)
        await self.say("translate hola to english")
        self.assertEqual(self.bot_posts(), [])


class FlagTest(PackCase):
    async def react(self, message: dict, emoji: str, user_id: int) -> None:
        self.bot._connection.parse_message_reaction_add({
            "user_id": str(user_id), "channel_id": self.text["id"], "message_id": message["id"], "guild_id": str(self.guild_id),
            "member": self.member_json[user_id], "emoji": {"id": None, "name": emoji}, "burst": False, "type": 0, "message_author_id": message["author"]["id"],
        })
        await self.settle()

    async def test_a_flag_translates_the_message_and_the_reply_goes_after_the_delete_time(self):
        original = await self.say("hola a todos")
        await self.react(original, BRITAIN, self.mod_id)
        post = self.bot_posts()[0]
        self.assertEqual(post["content"], f"{BRITAIN} **English (UK)** (from Spanish)\n[en-GB] hola a todos")
        self.assertEqual(post["message_reference"]["message_id"], int(original["id"]))
        timers = await self.bot.timers.list_kind(DELETE_KIND, self.guild_id)
        self.assertEqual(len(timers), 1)
        self.assertAlmostEqual(timers[0]["due_at"], time.time() + 120, delta=10)
        await self.bot.timers.run_due(now=time.time() + 130)
        deleted = [path for method, path, body in self.net.calls if method == "DELETE" and path == "/channels/{channel_id}/messages/{message_id}"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(await self.bot.timers.list_kind(DELETE_KIND, self.guild_id), [])
        self.assert_no_errors()

    async def test_one_translation_while_it_stays_up(self):
        original = await self.say("hola")
        await self.react(original, USA, self.mod_id)
        await self.react(original, USA, self.admin_id)
        self.assertEqual(len(self.bot_posts()), 1)
        self.assertEqual(len(self.deepl_calls), 1)
        self.pack.flags._marks[(int(original["id"]), "EN-US")] -= 121  # its delete time has passed
        await self.react(original, USA, self.admin_id)
        self.assertEqual(len(self.bot_posts()), 2)
        await self.react(original, SPAIN, self.admin_id)
        self.assertEqual(len(self.bot_posts()), 3, "another language is another translation")

    async def test_kept_translations_are_marked_for_good_and_get_no_timer(self):
        await self.switch(flag_delete_seconds=0)
        original = await self.say("hola")
        await self.react(original, USA, self.mod_id)
        self.pack.flags._marks[(int(original["id"]), "EN-US")] -= 100000
        await self.react(original, USA, self.admin_id)
        self.assertEqual(len(self.bot_posts()), 1)
        self.assertEqual(await self.bot.timers.list_kind(DELETE_KIND, self.guild_id), [])

    async def test_the_same_language_posts_nothing_and_stays_quiet(self):
        def same(call):
            body = json.loads(call["data"])
            self.deepl_calls.append(body)
            return reply({"translations": [{"text": body["text"][0], "detected_source_language": "EN"}]})
        self.web.answer(tr.FREE_URL, same)
        original = await self.say("hello everyone")
        await self.react(original, USA, self.mod_id)
        await self.react(original, USA, self.admin_id)
        self.assertEqual(self.bot_posts(), [])
        self.assertEqual(len(self.deepl_calls), 1)

    async def test_a_failure_lets_a_later_reaction_try_again(self):
        self.web.answer(tr.FREE_URL, reply({}, 503))
        original = await self.say("hola")
        await self.react(original, USA, self.mod_id)
        self.web.answer(tr.FREE_URL, deepl(calls=self.deepl_calls))
        await self.react(original, USA, self.admin_id)
        self.assertEqual(len(self.bot_posts()), 1)

    async def test_someone_over_the_limit_is_passed_over(self):
        for _ in range(5):
            self.assertFalse(self.pack.throttle.limited(self.mod_id))
        original = await self.say("hola")
        await self.react(original, USA, self.mod_id)
        self.assertEqual(self.bot_posts(), [])
        await self.react(original, USA, self.admin_id)
        self.assertEqual(len(self.bot_posts()), 1, "the limit didn't mark the message")

    async def test_other_emojis_the_switch_and_channels_left_out(self):
        original = await self.say("hola")
        await self.react(original, "⭐", self.mod_id)
        await self.switch(flag_translations=False)
        await self.react(original, USA, self.mod_id)
        await self.switch(flag_translations=True, ignored_channels=[int(self.text["id"])])
        await self.react(original, USA, self.mod_id)
        self.assertEqual(self.bot_posts(), [])
        self.assertEqual(self.deepl_calls, [])


class MenuTest(PackCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.sync_server_commands(self.guild_id)

    async def use_menu(self, target: dict, *, locale: str = "en-US") -> list[dict]:
        mark = len(self.net.calls)
        payload = self.interaction_payload(2, {"id": str(new_id()), "name": "Translate", "type": 3, "target_id": target["id"],
                                               "resolved": {"messages": {target["id"]: target}}, "guild_id": str(self.guild_id)}, user_id=self.member_id)
        payload["locale"] = locale
        self.bot._connection.parse_interaction_create(payload)
        await self.settle()
        self.assert_no_errors()
        return [body for method, path, body in self.net.calls[mark:] if method == "POST" and "webhook" in path or path.endswith("/callback")]

    async def test_it_answers_privately_in_the_members_discord_language(self):
        target = self.net.message_json(int(self.text["id"]), {"content": "good morning"})
        sent = await self.use_menu(target, locale="fr")
        self.assertEqual(self.deepl_calls[0]["target_lang"], "fr")
        followup = sent[-1]
        self.assertEqual(followup["content"], "\U0001f310 **Spanish → French**\n[fr] good morning")
        self.assertEqual(followup["flags"] & 64, 64)

    async def test_a_saved_language_comes_first(self):
        await self.pack.db.run("INSERT INTO translate_languages (guild_id, user_id, language, updated_at) VALUES (?, ?, 'JA', 1)", (self.guild_id, self.member_id))
        await self.use_menu(self.net.message_json(int(self.text["id"]), {"content": "good morning"}), locale="fr")
        self.assertEqual(self.deepl_calls[0]["target_lang"], "ja")

    async def test_a_message_with_no_text(self):
        sent = await self.use_menu(self.net.message_json(int(self.text["id"]), {"content": ""}))
        self.assertEqual(sent[-1]["data"]["content"], "That message has no text to translate.")
        self.assertEqual(self.deepl_calls, [])

    async def test_switched_off_it_says_so(self):
        await self.switch(enabled=False)
        sent = await self.use_menu(self.net.message_json(int(self.text["id"]), {"content": "hola"}))
        self.assertIn("switched off", sent[-1]["data"]["content"])


class ExportTest(PackCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.sync_server_commands(self.guild_id)
        self.files: list[tuple[str, bytes]] = []
        network = self.net.webhook

        async def capture(adapter, route, session=None, **kwargs):
            for file in kwargs.get("files") or []:
                self.files.append((file.filename, file.fp.getvalue()))
            return await network(adapter, route, session, **kwargs)
        self.net.webhook = capture

    def emojis(self, *rows: tuple[str, bool]) -> list[int]:
        ids = [new_id() for _ in rows]
        self.bot._connection.parse_guild_emojis_update({"guild_id": str(self.guild_id), "emojis": [
            {"id": str(emoji_id), "name": name, "animated": animated, "available": True, "managed": False, "require_colons": True, "roles": []}
            for emoji_id, (name, animated) in zip(ids, rows, strict=True)]})
        return ids

    async def run_export(self, user_id: int | None = None) -> list[dict]:
        mark = len(self.net.calls)
        data = {"id": str(new_id()), "name": "export_emojis", "type": 1, "options": [], "guild_id": str(self.guild_id)}
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id or self.owner_id))
        await self.settle()
        self.assert_no_errors()
        return [body for _, _, body in self.net.calls[mark:]]

    async def test_every_emoji_goes_in_the_zip_by_what_its_bytes_are(self):
        party, spin, again = self.emojis(("party", False), ("spin", True), ("Party", False))
        files = {party: b"\x89PNG party", spin: b"RIFF\x00\x00\x00\x00WEBP spin", again: b"\x89PNG again"}

        async def cdn(url, **kwargs):
            return files[int(url.split("/")[-1].split(".")[0])]
        self.bot.http.get_from_cdn = cdn
        sent = await self.run_export()
        self.assertEqual([name for name, _ in self.files], ["custom_emojis.zip"])
        archive = zipfile.ZipFile(io.BytesIO(self.files[0][1]))
        self.assertEqual(sorted(archive.namelist()), ["Party~1.png", "party.png", "spin.webp"])
        self.assertEqual(archive.read("spin.webp"), files[spin])
        self.assertEqual(sent[-1]["content"], "Exported 3 custom emoji(s) to ZIP:")
        self.assertEqual(sent[-1]["flags"] & 64, 64, "only the person who asked gets the file")

    async def test_a_large_set_goes_in_several_zips(self):
        self.emojis(("a", False), ("b", False), ("c", False))

        async def cdn(url, **kwargs):
            return b"\x89PNG" + bytes(700 * 1024)
        self.bot.http.get_from_cdn = cdn
        with mock.patch.object(translation_module, "EXPORT_PART_BYTES", 2 * 1024 * 1024):
            sent = await self.run_export()
        self.assertEqual([name for name, _ in self.files], ["custom_emojis.zip", "custom_emojis-2.zip"])
        self.assertEqual(sum(len(zipfile.ZipFile(io.BytesIO(data)).namelist()) for _, data in self.files), 3)
        self.assertEqual(sent[-1]["content"], "Exported 3 custom emoji(s) in 2 ZIP files. This is the last one.")

    async def test_failed_downloads_are_left_out_and_none_says_so(self):
        self.emojis(("gone", False))

        async def cdn(url, **kwargs):
            raise discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "gone")
        self.bot.http.get_from_cdn = cdn
        sent = await self.run_export()
        self.assertIn("No emojis could be downloaded.", sent[-1]["content"])
        self.assertEqual(self.files, [])

    async def test_a_download_that_breaks_or_stalls_is_left_out_and_the_rest_still_go(self):
        # Only Discord's own errors were caught, so a dropped connection on one emoji ended the export with nothing sent.
        for label, failure in (("dropped", aiohttp.ClientConnectionError("Connection reset by peer")), ("timed out", asyncio.TimeoutError()), ("stalled", None)):
            with self.subTest(label):
                good, bad, other = self.emojis(("good", False), ("flaky", False), ("other", True))

                async def cdn(url, good=good, bad=bad, failure=failure):
                    emoji_id = int(url.split("/")[-1].split(".")[0])
                    if emoji_id == bad:
                        if failure is None:
                            await asyncio.sleep(30)
                        raise failure
                    return b"\x89PNG good" if emoji_id == good else b"GIF89a other"
                self.bot.http.get_from_cdn = cdn
                self.files.clear()
                with mock.patch.object(translation_module, "EMOJI_DOWNLOAD_SECONDS", 0.2):
                    sent = await self.run_export()
                names = sorted(name for _, data in self.files for name in zipfile.ZipFile(io.BytesIO(data)).namelist())
                self.assertEqual(names, ["good.png", "other.gif"])
                self.assertEqual(sent[-1]["content"], "Exported 2 custom emoji(s) to ZIP. 1 couldn't be downloaded.")

    async def test_no_emojis_and_no_administrator(self):
        sent = await self.run_export()
        self.assertIn("This server has no custom emojis.", json.dumps(sent))
        self.emojis(("party", False))
        sent = await self.run_export(user_id=self.mod_id)
        self.assertEqual(self.files, [])
        self.assertIn("Administrator", json.dumps(sent))


class DeleteTimerTest(PackCase):
    async def test_a_translation_already_gone_counts_as_deleted(self):
        await self.bot.timers.schedule(DELETE_KIND, time.time() - 1, {"channel_id": int(self.text["id"]), "message_id": new_id()}, guild_id=self.guild_id)
        await self.bot.timers.run_due()
        self.assertEqual(await self.bot.timers.list_kind(DELETE_KIND), [])
        self.assert_no_errors()


class MyDataTest(BotTest):
    modules = load_all()

    async def test_a_members_language_shows_and_deletes_by_server(self):
        await self.db.run("INSERT INTO translate_languages (guild_id, user_id, language, updated_at) VALUES (?, ?, 'ES', 1)", (self.guild.id, self.member.id))
        await self.db.run("INSERT INTO translate_languages (guild_id, user_id, language, updated_at) VALUES (999, ?, 'FR', 1)", (self.member.id,))
        await self.db.run("INSERT INTO translate_languages (guild_id, user_id, language, updated_at) VALUES (?, ?, 'DE', 1)", (self.guild.id, self.mod.id))
        self.assertIn("translation", BY_KEY)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(self.member.id, "member", None))
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        data = await (await client.get("/api/my-data")).json()
        here = next(server for server in data["servers"] if str(server["id"]) == str(self.guild.id))
        self.assertIn({"key": "translation", "label": "Translation language", "count": 1}, here["sections"])
        download = await (await client.get("/api/my-data/download")).json()
        self.assertEqual({row["language"] for row in download["tables"]["translate_languages"]}, {"ES", "FR"})
        response = await client.post("/api/my-data/delete", json={"guild_id": str(self.guild.id), "section": "translation"})
        self.assertEqual(response.status, 200, await response.text())
        rows = await self.db.all("SELECT guild_id, user_id FROM translate_languages ORDER BY language")
        self.assertEqual(rows, [{"guild_id": self.guild.id, "user_id": self.mod.id}, {"guild_id": 999, "user_id": self.member.id}])
