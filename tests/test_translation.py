"""Translation and conversions: the unit and currency tables, the readers that find a conversion in a sentence, the language and flag
tables, the DeepL client with its chunks, pacing and retries, and the pack's commands."""
from __future__ import annotations

import json
import time
import unittest

from captainguild.core import conversions as convert
from captainguild.core import translator as tr
from captainguild.core.outside import OutsideError
from captainguild.modules.translation import RATE_SOURCES, RATES_KEY, Translation, clean_rates, emoji_file_type
from tests.fake_outside import FakeOutside, reply
from tests.fakes import BotTest, FakeChannel

RATES = {"date": "2026-11-06", "usd": {"usd": 1, "eur": 0.9, "cad": 1.4, "gbp": 0.8, "btc": 0.00001, "doge": 0, "jpy": 150, "pkr": 280}}
# ISO 4217's codes for the currencies in use, which the rate list holds beside the cryptocurrencies.
ISO_CODES = set("""aed afn all amd ang aoa ars aud awg azn bam bbd bdt bgn bhd bif bmd bnd bob brl bsd btn bwp byn bzd cad cdf chf clp cny cop crc cup
cve czk djf dkk dop dzd egp ern etb eur fjd fkp gbp gel ghs gip gmd gnf gtq gyd hkd hnl htg huf idr ils inr iqd irr isk jmd jod jpy kes kgs
khr kmf kpw krw kwd kyd kzt lak lbp lkr lrd lsl lyd mad mdl mga mkd mmk mnt mop mru mur mvr mwk mxn myr mzn nad ngn nio nok npr nzd omr pab
pen pgk php pkr pln pyg qar ron rsd rub rwf sar sbd scr sdg sek sgd shp sle sos srd ssp stn syp szl thb tjs tmt tnd top try ttd twd tzs uah
ugx usd uyu uzs ves vnd vuv wst xaf xcd xof xpf yer zar zmw zwl""".split())
NAMES = {"usd": "US Dollar", "eur": "Euro", "cad": "Canadian Dollar", "gbp": "British Pound", "btc": "Bitcoin", "jpy": "Japanese Yen"}


def deepl(detected: str = "ES", calls: list | None = None):
    """A stand-in for DeepL that answers each chunk with its target code in front."""
    def answer(call):
        body = json.loads(call["data"])
        if calls is not None:
            calls.append(body)
        return reply({"translations": [{"text": f"[{body['target_lang']}] {body['text'][0]}", "detected_source_language": detected}]})
    return answer


def rates_site(web: FakeOutside, data: dict | None = None, names: dict | None = None) -> FakeOutside:
    web.answer(f"{RATE_SOURCES[0]}/currencies/usd.json", data or RATES)
    web.answer(f"{RATE_SOURCES[0]}/currencies.json", names or NAMES)
    return web


class UnitTablesTest(unittest.TestCase):
    def test_each_kind_converts_through_its_base(self):
        cases = [
            (5, "km", "mi", 3.1068559611866697), (1, "in", "mm", 25.4), (1, "lb", "kg", 0.45359237), (1, "gal", "l", 3.785411784),
            (1, "acre", "sq m", 4046.8564224), (60, "mph", "km/h", 96.56064), (90, "min", "hr", 1.5), (1, "KiB", "bytes", 1024),
            (1, "atm", "kPa", 101.325), (1, "kWh", "J", 3600000.0), (1, "hp", "W", 745.69987158), (1, "kgf", "N", 9.80665),
            (1, "ft-lb", "Nm", 1.3558179483314), (180, "deg", "rad", 3.141592653589793), (1, "Gbps", "Mbps", 1000.0),
            (10, "km/L", "mpg", 23.521458), (60, "mm/s", "mm/min", 3600.0), (1, "cc/min", "mm³/s", 16.666666666666668),
            (1, "lb/in³", "g/cm³", 27.679904710191),
        ]
        for amount, source, target, expected in cases:
            self.assertAlmostEqual(convert.convert_units(amount, source, target), expected, places=5, msg=f"{amount} {source} to {target}")

    def test_temperatures_go_through_kelvin(self):
        self.assertAlmostEqual(convert.convert_units(100, "°C", "°F"), 212.0)
        self.assertAlmostEqual(convert.convert_units(32, "fahrenheit", "celsius"), 0.0)
        self.assertAlmostEqual(convert.convert_units(0, "K", "C"), -273.15)

    def test_kilograms_per_cubic_meter_are_a_thousandth_of_grams_per_cubic_centimeter(self):
        # The old table had kg/m³ as a thousand times g/cm³, a millionfold off.
        self.assertAlmostEqual(convert.convert_units(1, "g/cm³", "kg/m³"), 1000.0)
        self.assertAlmostEqual(convert.convert_units(1240, "kg/m³", "g/cm³"), 1.24)

    def test_a_name_two_tables_share_means_the_first_that_holds_both(self):
        self.assertAlmostEqual(convert.convert_units(1, "m/s", "km/h"), 3.6)  # speed, where "ms" is meters per second
        self.assertAlmostEqual(convert.convert_units(1500, "ms", "s"), 1.5)  # time, since speed has no "s"

    def test_a_micro_sign_means_micro(self):
        # Keeping only the letters a to z read 200 µm as 200 meters.
        for typed in ("\u00b5m", "\u03bcm", "um"):
            self.assertAlmostEqual(convert.convert_units(200, typed, "mm"), 0.2, msg=typed)
        self.assertEqual(convert.normalize_unit("\u00b5m"), "um")
        self.assertIn("\u00b5m", convert.UNITS_HELP, "the help names a unit it reads")

    def test_squares_and_cubes_are_areas_and_volumes(self):
        # The old reading dropped the mark and converted m² as meters.
        for amount, source, target, expected in (
            (5, "m\u00b2", "ft\u00b2", 53.8195520835486), (5, "m2", "ft2", 53.8195520835486), (10, "in\u00b2", "cm\u00b2", 64.516),
            (1000, "mm\u00b3", "cm\u00b3", 1.0), (1, "m\u00b3", "l", 1000.0), (1, "ft3", "in3", 1728.0), (2, "km\u00b2", "ha", 200.0),
        ):
            self.assertAlmostEqual(convert.convert_units(amount, source, target), expected, places=4, msg=f"{source} to {target}")

    def test_a_ratio_keeps_its_cube_and_its_slash(self):
        self.assertAlmostEqual(convert.convert_units(1, "lb/ft\u00b3", "kg/m\u00b3"), 16.01846337396, places=6)
        self.assertAlmostEqual(convert.convert_units(1, "lb/in\u00b3", "g/cm\u00b3"), 27.679904710191, places=6)
        self.assertAlmostEqual(convert.convert_units(2, "mm\u00b3/s", "cc/min"), 0.12, places=6)
        self.assertAlmostEqual(convert.convert_units(1, "mm3/s", "cc/min"), 0.06, places=6)

    def test_pound_feet_are_torque(self):
        # lb-ft read as lbft, which only the density table held.
        self.assertAlmostEqual(convert.convert_units(50, "lb-ft", "lb-in"), 600.0, places=6)
        self.assertAlmostEqual(convert.convert_units(10, "lb-ft", "Nm"), 13.558179483314, places=6)
        self.assertIsNone(convert.convert_units(1, "gcm", "kgm"), "a density needs its slash")

    def test_a_symbol_whose_case_changes_the_unit(self):
        # Lowercasing first read mW as megawatts, Cal as small calories and Mb as megabytes.
        for amount, source, target, expected in (
            (500, "mW", "W", 0.5), (1, "MW", "kW", 1000.0), (200, "Cal", "kJ", 836.8), (1, "cal", "J", 4.184), (100, "Mb", "MB", 12.5),
            (8, "Gb", "GB", 1.0), (1, "MBps", "Mbps", 8.0), (1, "Mbps", "kbps", 1000.0), (16, "b", "B", 2.0), (1, "KB", "bytes", 1000.0), (1, "kb", "bytes", 1000.0),
        ):
            self.assertAlmostEqual(convert.convert_units(amount, source, target), expected, places=6, msg=f"{source} to {target}")
        self.assertIsNone(convert.convert_units(5, "Mm", "in"), "megameters, or millimeters typed with a slip")

    def test_a_unit_holding_a_stray_number_is_no_unit(self):
        # "from 200 C to 210 C" read the target as C, and "3/4 inch" left "/4 inch" as the unit.
        for source, target in (("C", "210 C"), ("mm/s", "300 mm/s"), ("/4 inch", "mm"), ("1/2 cup", "ml"), ("X1C", "mm"), ("mm", "M4 screws")):
            self.assertIsNone(convert.convert_units(1, source, target), f"{source} to {target}")

    def test_units_of_different_kinds_give_nothing(self):
        self.assertIsNone(convert.convert_units(1, "kg", "km"))
        self.assertIsNone(convert.convert_units(1, "parts", "pla"))

    def test_the_suggestions_cover_every_kind(self):
        for unit in ("°C", "fl oz", "imp gal", "sq ft", "km/h", "KiB", "mmHg", "kWh", "hp", "dyne", "ft-lb", "arcsec", "Gbps", "imp mpg", "mm³/s", "lb/ft³"):
            self.assertIn(unit, convert.UNIT_CHOICES)
        self.assertEqual(convert.UNIT_CHOICES, sorted(convert.UNIT_CHOICES))


class CurrencyWordsTest(unittest.TestCase):
    def test_names_slang_and_codes(self):
        rates = clean_rates(RATES["usd"])
        for typed, code in (("bucks", "usd"), ("Loonies", "cad"), ("moose pesos", "cad"), ("quid", "gbp"), ("U.S. Dollars", "usd"), ("bitcoin", "btc"),
                            ("freedom farthings", "usd"), ("pkr", "pkr"), ("PKR ", "pkr")):
            self.assertEqual(convert.currency_code(typed, rates), code, typed)
        self.assertIsNone(convert.currency_code("parts", rates))
        self.assertIsNone(convert.currency_code("pkr", None), "a code needs the rates, and a name doesn't")
        self.assertEqual(convert.currency_code("euro", None), "eur")

    def test_no_name_stands_for_another_currencys_code(self):
        # "bob" was slang for pounds, though BOB is the Bolivian boliviano's code, which the rate list holds.
        self.assertEqual(convert.currency_code("bob", {"bob": 6.9, "gbp": 0.8}), "bob")
        self.assertIsNone(convert.currency_code("bobs", {"bob": 6.9, "gbp": 0.8}))
        clashes = {name: code for name, code in convert.CURRENCY_ALIASES.items() if len(name) == 3 and name.isalpha() and name != code and name in ISO_CODES}
        self.assertEqual(clashes, {})

    def test_labels_show_the_typed_name_with_the_code(self):
        self.assertEqual(convert.currency_label("bucks", "usd", NAMES), "bucks (USD - US Dollar)")
        self.assertEqual(convert.currency_label("USD", "usd", NAMES), "USD - US Dollar")
        self.assertEqual(convert.currency_label("pepe", "pepe", {}), "PEPE - Pepe")
        self.assertEqual(convert.currency_label("zzz", "zzz", {}), "ZZZ")
        self.assertEqual(convert.currency_display("eur", NAMES), "EUR - Euro")

    def test_the_jokes(self):
        self.assertEqual(convert.currency_jokes("moose pesos", "usd"), " \U0001face")
        self.assertEqual(convert.currency_jokes("cad", "freedom farthings"), " \U0001f985\U0001f1fa\U0001f1f8")
        self.assertEqual(convert.currency_jokes("moose pesos", "freedom farthings"), " \U0001face \U0001f985\U0001f1fa\U0001f1f8")
        self.assertEqual(convert.currency_jokes("bucks", "euros"), "")

    def test_a_small_figure_keeps_its_digits(self):
        self.assertEqual(convert.figure(1234.5, 2), "1,234.50")
        self.assertEqual(convert.figure(0.0000105, 2), "0.0000105")  # two places would show 0.00 for a few cents of bitcoin
        self.assertEqual(convert.figure(3.1068559611866697, 10), "3.1068559612")
        self.assertEqual(convert.figure(1.602176634e-19, 10), "1.602e-19")
        self.assertEqual(convert.figure(0, 2), "0.00")
        self.assertEqual(convert.figure(9.46e36, 10), "9.460000e+36")


class SentenceReadersTest(unittest.TestCase):
    def asked(self, reader, text):
        found = reader(text)
        return None if found is None else tuple(found)

    def test_currency_in_words(self):
        for text, expected in (
            ("how much is 20 bucks in euros?", ("20", "bucks", "euros")),
            ("convert 50 pounds to dollars", ("50", "pounds", "dollars")),
            ("1,234.56 canadian dollars into us dollars", ("1,234.56", "canadian dollars", "us dollars")),
            ("100 USD to EUR please", ("100", "USD", "EUR please")),
            ("-5 eur to usd", ("-5", "eur", "usd")),
        ):
            self.assertEqual(self.asked(convert.currency_sentence, text), expected, text)
        self.assertIsNone(convert.currency_sentence("100 usd"))
        self.assertIsNone(convert.currency_sentence("100usd to eur"), "the amount and the name need a space between")

    def test_a_joining_word_inside_a_name_isnt_one(self):
        # The old reader took "in" out of "bitcoin" and asked about "bitco".
        self.assertEqual(self.asked(convert.currency_sentence, "1 bitcoin to usd"), ("1", "bitcoin", "usd"))
        self.assertEqual(self.asked(convert.currency_sentence, "1 litecoin in eur"), ("1", "litecoin", "eur"))

    def test_an_amount_needs_a_digit(self):
        # The old reader took "..." for the amount, failed to read it, and gave up on the message.
        self.assertEqual(self.asked(convert.currency_sentence, "wait... 5 dollars to euros"), ("5", "dollars", "euros"))
        self.assertEqual(self.asked(convert.unit_sentence, "hmm... 5 km to mi"), ("5", "km", "mi"))

    def test_the_first_amount_with_a_conversion_after_it_decides(self):
        self.assertEqual(self.asked(convert.currency_sentence, "5 dollars to euros and 10 euros to yen"), ("5", "dollars", "euros and 10 euros to yen"))

    def test_two_codes(self):
        for text, expected in (("100 usd eur", ("100", "usd", "eur")), ("100USD to EUR", ("100", "USD", "EUR")), ("100 usd=eur", ("100", "usd", "eur")),
                               ("convert 5 gbp into usd", ("5", "gbp", "usd")), ("Price: 100 EUR, about 108 USD", None)):
            self.assertEqual(self.asked(convert.currency_codes, text), expected, text)

    def test_a_code_is_a_whole_word(self):
        # The old reader read "10gbp to usdt" as pounds to US dollars, and letters inside any word as codes.
        self.assertIsNone(convert.currency_codes("10gbp to usdt"))
        self.assertIsNone(convert.currency_codes("I need 3 cables for 5 printers"))
        self.assertIsNone(convert.currency_codes("100 usdeur"))

    def test_units_in_words(self):
        for text, expected in (
            ("convert 5 km to miles", ("5", "km", "miles")), ("5 in to cm", ("5", "in", "cm")), ("5km to mi", ("5", "km", "mi")),
            ("350F in C", ("350", "F", "C")), ("10 fl oz to ml", ("10", "fl oz", "ml")), ("temp is 22 °C to °F", ("22", "°C", "°F")),
            ("20 m/s to km/h", ("20", "m/s", "km/h")), ("2 hours to minutes please", ("2", "hours", "minutes please")),
        ):
            self.assertEqual(self.asked(convert.unit_sentence, text), expected, text)
        for text in ("5 km to", "how many ft in 3 m", "use 0.2 mm layers"):
            self.assertIsNone(convert.unit_sentence(text), text)

    def test_a_long_message_is_read_in_time(self):
        # The old patterns took minutes on a message like these, holding up the whole bot.
        for text in ("1" * 4000, "1" + " " * 3998 + "x", "1 " * 2000, "1 to " * 800, "12 abc " * 570, "1 in " * 800):
            started = time.perf_counter()
            convert.currency_sentence(text)
            convert.currency_codes(text)
            convert.unit_sentence(text)
            self.assertLess(time.perf_counter() - started, 0.5, text[:20])

    def test_an_amount_is_the_whole_number_written(self):
        # The old reading stopped at a slash, took a range's dash for a minus sign, and read every comma as a thousands separator.
        for text, expected in (("3/4 inch", ["3/4"]), ("on 11/6/2026 it was 5 km", ["5"]), ("190-220 C", ["190", "220"]), ("my X1C did 5 km", ["5"]),
                               ("-5 eur", ["-5"]), ("v1.5 of it", []), ("1,75 mm", ["1,75"])):
            self.assertEqual([found.group() for found in convert.amounts(text)], expected, text)
        for typed, value in (("3/4", 0.75), ("-1/2", -0.5), ("1,75", 1.75), ("1,5", 1.5), ("1,000", 1000.0), ("1,000.5", 1000.5), ("1.000,5", 1000.5),
                             ("1.234.567", 1234567.0), ("12,345,678", 12345678.0), ("0.4", 0.4), (".5", 0.5), ("5.", 5.0)):
            self.assertEqual(convert.parse_amount(typed), value, typed)
        for typed in ("1/0", "1,2,3", "1.2.3", "1,5/2", "1.2,3.4"):
            self.assertIsNone(convert.parse_amount(typed), typed)

    def test_a_mixed_number_is_one_amount(self):
        # "1 1/2 inch" read the 1 as the amount and "1/2 inch" as the unit, so it got no answer. The old bot answered for 1 inch.
        for text, expected in (("1 1/2 inch to mm", ["1 1/2"]), ("2-3/4 in", ["2-3/4"]), ("-1 1/2 C", ["-1 1/2"]), ("on 10 3/4/2026", ["10"]),
                               ("1/4-1/2 inch", ["1/4", "1/2"]), ("190-220 C", ["190", "220"])):
            self.assertEqual([found.group() for found in convert.amounts(text)], expected, text)
        for typed, value in (("1 1/2", 1.5), ("1-1/2", 1.5), ("2  3/4", 2.75), ("-1 1/2", -1.5), ("0 1/2", 0.5)):
            self.assertEqual(convert.parse_amount(typed), value, typed)
        self.assertIsNone(convert.parse_amount("1 1/0"))
        self.assertEqual(convert.unit_sentence("cut 1 1/2 inch to mm"), convert.Asked("1 1/2", "inch", "mm"))

    def test_amounts(self):
        self.assertEqual([found.group() for found in convert.amounts("a ... 1,000.5 b -3 c")], ["1,000.5", "-3"])
        self.assertEqual(convert.parse_amount("1,000.5"), 1000.5)
        self.assertIsNone(convert.parse_amount("1.2.3"))
        self.assertIsNone(convert.parse_amount("1" + "0" * 400), "a number that won't fit isn't an amount")


class LanguageTablesTest(unittest.TestCase):
    def test_the_tables_hold_every_entry(self):
        self.assertEqual(len(tr.LANGUAGES), 36)
        self.assertEqual(len(tr.FLAG_LANGUAGES), 86)
        self.assertEqual(len(tr.DISCORD_LOCALES), 29)
        self.assertEqual(len(tr.SOURCE_LANGUAGES), 32)
        for code, _, _ in tr.LANGUAGES:
            self.assertIn(code, tr.TARGET_CODES)

    def test_names_codes_and_other_names(self):
        for typed, code in (("Spanish", "ES"), ("es", "ES"), ("deutsch", "DE"), ("español", "ES"), ("mandarin", "ZH"), ("brazilian", "PT-BR"),
                            ("english", "EN-US"), ("en", "EN-US"), ("pt", "PT-BR"), ("zh-tw", "ZH-HANT"), ("Latin American  Spanish", "ES-419"), ("EN-GB", "EN-GB")):
            self.assertEqual(tr.resolve_target(typed), code, typed)
        self.assertIsNone(tr.resolve_target("klingon"))

    def test_a_source_is_a_base_language(self):
        self.assertEqual(tr.resolve_source("english"), "EN")
        self.assertEqual(tr.resolve_source("brazilian"), "PT")
        self.assertEqual(tr.resolve_source("zh-tw"), "ZH")
        self.assertEqual(tr.resolve_source("auto"), "auto")
        self.assertEqual(tr.resolve_source("klingon"), "auto")

    def test_flags(self):
        for flag, code in (("\U0001f1fa\U0001f1f8", "EN-US"), ("\U0001f1ec\U0001f1e7", "EN-GB"), ("\U0001f1f2\U0001f1fd", "ES-419"), ("\U0001f1e7\U0001f1f7", "PT-BR"),
                           ("\U0001f1f9\U0001f1fc", "ZH-HANT"), ("\U0001f1f8\U0001f1e6", "AR"), ("\U0001f1fa\U0001f1e6", "UK")):
            self.assertEqual(tr.flag_language(flag), code, flag)
        self.assertIsNone(tr.flag_language("⭐"))
        self.assertIsNone(tr.flag_language("<:party:1>"))
        self.assertIsNone(tr.flag_language("\U0001f1e6\U0001f1f6"), "Antarctica has no language")

    def test_names_for_detected_sources(self):
        self.assertEqual(tr.language_name("EN"), "English")
        self.assertEqual(tr.language_name("ZH-HANS"), "Chinese (Simplified)")
        self.assertEqual(tr.language_name("en-us"), "English (US)")
        self.assertEqual(tr.language_name("auto"), "AUTO")
        self.assertEqual(tr.language_name(None), "Unknown")

    def test_a_discord_language_names_a_target(self):
        self.assertEqual(tr.locale_target("es-419"), "ES-419")
        self.assertEqual(tr.locale_target("hi"), None, "DeepL can't translate into Hindi")


class RequestTest(unittest.TestCase):
    def test_text_and_a_target(self):
        self.assertEqual(tr.read_request("translate hola amigo to english", False), tr.Request("hola amigo", "EN-US", False))
        self.assertEqual(tr.read_request("Translate: bonjour", False), tr.Request("bonjour", None, False))
        self.assertEqual(tr.read_request("translate I want to go to Spain to japanese", False), tr.Request("I want to go to Spain", "JA", False))
        self.assertEqual(tr.read_request("translate go to spain", False), tr.Request("go to spain", None, False), "Spain isn't a language")

    def test_only_a_target_asks_for_the_replied_message(self):
        self.assertEqual(tr.read_request("translate to french", True), tr.Request("to french", "FR", True))
        self.assertEqual(tr.read_request("translate into german", False), tr.Request("into german", "DE", True))
        self.assertEqual(tr.read_request("translate", True), tr.Request("", None, True))

    def test_what_isnt_a_request(self):
        for content, is_reply in (("translate", False), ("translates this", False), ("translate-shell", False), ("please translate this", False)):
            self.assertIsNone(tr.read_request(content, is_reply), content)

    def test_a_forwarded_messages_text(self):
        class Snapshot:
            content = "  hola  "

        class Forwarded:
            content = ""
            message_snapshots = [Snapshot()]
        self.assertEqual(tr.message_text(Forwarded()), "hola")


class ChunkingTest(unittest.TestCase):
    def test_chunks_put_back_together_give_the_text(self):
        text = ("First paragraph sentence one. Sentence two!\n\n" + "Word " * 400 + "\n" + "中文句子。" * 300).strip()
        pieces = tr.chunk_text(text, tr.CHUNK_LIMIT)
        self.assertEqual("".join(chunk + gap for chunk, gap in pieces), text)
        self.assertTrue(all(len(chunk) <= tr.CHUNK_LIMIT for chunk, _ in pieces))

    def test_a_paragraph_break_wins_over_a_sentence_end(self):
        pieces = tr.chunk_text("a" * 50 + ". " + "b" * 30 + "\n\n" + "c" * 30, 100)
        self.assertEqual(pieces[0], ("a" * 50 + ". " + "b" * 30, "\n\n"))

    def test_cjk_sentences_split_after_their_end(self):
        pieces = tr.chunk_text("一" * 40 + "。" + "二" * 40, 60)
        self.assertEqual(pieces[0][0], "一" * 40 + "。")

    def test_a_short_translation_is_one_plain_message(self):
        self.assertEqual(tr.messages_for("header\nhola", "title", "hola"), [{"content": "header\nhola"}])

    def test_a_long_translation_goes_in_embeds_within_discords_totals(self):
        text = "word " * 600
        sent = tr.messages_for("header\n" + text, "title", text.strip())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["embeds"][0].title, "title")
        huge = ("Sentence number one is here. " * 600).strip()  # past 6,000 characters, which one message's embeds can't hold
        sent = tr.messages_for("header\n" + huge, "title", huge)
        self.assertGreater(len(sent), 1)
        for message in sent:
            self.assertLessEqual(sum(len(embed.description) + len(embed.title or "") for embed in message["embeds"]), tr.EMBEDS_TOTAL)
        self.assertEqual("".join(embed.description for message in sent for embed in message["embeds"]).replace(" ", ""), huge.replace(" ", ""))

    def test_error_texts(self):
        self.assertIn("rate-limited", tr.error_text({"status": 429}))
        self.assertIn("had a problem", tr.error_text({"status": 503}))
        self.assertIn("no text", tr.error_text({"status": 404}))
        self.assertEqual(tr.error_text({"status": 400, "message": "Bad target"}), "\U0001f310 Bad target")
        self.assertEqual(tr.error_text({"status": 400}), "\U0001f310 Unsupported language.")
        self.assertIn("Couldn't reach", tr.error_text({"status": None}))
        self.assertEqual(tr.error_text({"status": 418}), "\U0001f310 Translation failed (status 418).")
        self.assertLessEqual(len(tr.error_text({"status": 400, "message": "x" * 5000})), tr.SERVICE_MESSAGE_LIMIT + 2)


class LimitsTest(unittest.TestCase):
    def test_five_a_person_in_thirty_seconds(self):
        now = [1000.0]
        throttle = tr.Throttle(clock=lambda: now[0])
        self.assertEqual([throttle.limited(1) for _ in range(6)], [False] * 5 + [True])
        self.assertFalse(throttle.limited(2), "each person has their own")
        now[0] += 30.5
        self.assertFalse(throttle.limited(1))

    def test_a_flag_mark_lasts_as_long_as_its_translation(self):
        marks = tr.FlagMarks()
        marks.mark((1, "ES"), 100.0)
        self.assertTrue(marks.live((1, "ES"), 120, 219.0))
        self.assertFalse(marks.live((1, "ES"), 120, 221.0))
        self.assertTrue(marks.live((1, "ES"), None, 10000.0), "kept translations stay marked")
        self.assertFalse(marks.live((1, "FR"), None, 100.0))
        marks.unmark((1, "ES"))
        self.assertFalse(marks.live((1, "ES"), None, 100.0))
        for number in range(tr.FLAG_MARKS + 5):
            marks.mark((number, "ES"), 1.0)
        self.assertFalse(marks.live((0, "ES"), None, 1.0), "the oldest go first")
        self.assertTrue(marks.live((tr.FLAG_MARKS + 4, "ES"), None, 1.0))


class EngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.web = FakeOutside()
        self.slept: list[float] = []

        async def sleep(seconds):
            self.slept.append(seconds)
        self.engine = tr.Translator(self.web, sleep=sleep)
        self.engine.pace = 0

    async def test_a_request_looks_like_the_extensions(self):
        bodies: list = []
        self.web.answer(tr.FREE_URL, deepl(calls=bodies))
        result = await self.engine.translate("hola", "EN-US", "ES")
        self.assertEqual(result, {"ok": True, "text": "[en-US] hola", "source_lang": "ES", "target_lang": "EN-US"})
        body = bodies[0]
        self.assertEqual(body["target_lang"], "en-US", "an uppercase code comes back untranslated")
        self.assertEqual(body["source_lang"], "es")
        self.assertEqual(body["usage_type"], "Translate")
        self.assertEqual(body["app_information"]["app_build"], "chrome_web_store")
        headers = self.web.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "None")
        self.assertEqual(headers["Origin"], f"chrome-extension://{tr.EXTENSION_ID}")
        self.assertIn("Chrome/120", headers["User-Agent"])

    async def test_a_token_goes_to_the_pro_endpoint(self):
        self.web.answer(tr.PRO_URL, deepl())
        result = await self.engine.translate("hola", "DE", token="abc")
        self.assertTrue(result["ok"])
        self.assertEqual(self.web.calls[0]["headers"]["Authorization"], "Bearer abc")

    async def test_auto_sends_no_source_and_regional_targets_map(self):
        bodies: list = []
        self.web.answer(tr.FREE_URL, deepl(calls=bodies))
        await self.engine.translate("x", "ZH")
        await self.engine.translate("x", "PT")
        self.assertNotIn("source_lang", bodies[0])
        self.assertEqual([body["target_lang"] for body in bodies], ["zh-Hans", "pt-BR"])

    async def test_a_busy_answer_is_tried_again_with_a_growing_wait(self):
        answers = [reply({"message": "Too many"}, 429), reply({"message": "Too many"}, 429, {"Retry-After": "5"})]

        def respond(call):
            return answers.pop(0) if answers else deepl()(call)
        self.web.answer(tr.FREE_URL, respond)
        result = await self.engine.translate("hola", "EN-US")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.web.calls), 3)
        self.assertTrue(1.5 <= self.slept[0] <= 1.9, self.slept)
        self.assertEqual(self.slept[1], 5.0, "a longer Retry-After wins")

    async def test_busy_four_times_gives_up(self):
        self.web.answer(tr.FREE_URL, reply({}, 429))
        result = await self.engine.translate("hola", "EN-US")
        self.assertEqual(result["status"], 429)
        self.assertEqual(len(self.web.calls), tr.MAX_RETRIES + 1)

    async def test_a_wait_past_the_longest_ends_the_attempt(self):
        self.web.answer(tr.FREE_URL, reply({}, 429, {"Retry-After": "3600"}))
        result = await self.engine.translate("hola", "EN-US")
        self.assertEqual(result["status"], 429)
        self.assertEqual(len(self.web.calls), 1)
        self.assertEqual(self.slept, [])

    async def test_other_answers_come_back_at_once(self):
        for status, body, expected in ((400, {"message": "Bad language"}, "Bad language"), (503, {}, None)):
            web = FakeOutside().answer(tr.FREE_URL, reply(body, status))
            self.engine.outside = web
            result = await self.engine.translate("hola", "EN-US")
            self.assertEqual((result["status"], result["message"]), (status, expected))
            self.assertEqual(len(web.calls), 1)

    async def test_empty_text_and_empty_answers(self):
        self.assertEqual((await self.engine.translate("   ", "EN-US"))["status"], 404)
        self.web.answer(tr.FREE_URL, {"translations": [{"text": ""}]})
        self.assertEqual(await self.engine.translate("hola", "EN-US"), {"ok": False, "status": 200, "message": "Translation came back empty."})
        self.assertEqual((await self.engine.translate("hola", "XX"))["status"], 400)

    async def test_an_unreachable_service(self):
        self.web.answer(tr.FREE_URL, OutsideError("Could not reach the service."))
        self.assertEqual((await self.engine.translate("hola", "EN-US"))["status"], None)

    async def test_long_text_goes_in_paced_chunks_and_comes_back_whole(self):
        bodies: list = []
        self.web.answer(tr.FREE_URL, deepl(calls=bodies))
        self.engine.pace = 0.35
        text = "Sentence one is here. " * 120 + "\n\n" + "Another part. " * 50
        result = await self.engine.translate(text, "FR")
        self.assertTrue(result["ok"])
        self.assertGreater(len(bodies), 1)
        self.assertTrue(all(len(body["text"][0]) <= tr.CHUNK_LIMIT for body in bodies))
        self.assertEqual(result["text"].replace("[fr] ", ""), text.strip())
        self.assertGreaterEqual(len(self.slept), len(bodies) - 1, "each request after the first waits its turn")

    async def test_the_first_failing_chunk_ends_it(self):
        answers = [None, reply({}, 503)]

        def respond(call):
            answer = answers.pop(0) if answers else None
            return answer or deepl()(call)
        self.web.answer(tr.FREE_URL, respond)
        result = await self.engine.translate("Sentence. " * 300, "FR")
        self.assertEqual(result["status"], 503)
        self.assertEqual(len(self.web.calls), 2)

    async def test_input_past_the_limit_is_cut(self):
        bodies: list = []
        self.web.answer(tr.FREE_URL, deepl(calls=bodies))
        await self.engine.translate("a" * (tr.MAX_INPUT + 500), "FR")
        self.assertEqual(sum(len(body["text"][0]) for body in bodies), tr.MAX_INPUT)


class Case(BotTest):
    modules = [Translation]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pack = self.cog(Translation)
        self.web = rates_site(FakeOutside())
        self.web.answer(tr.FREE_URL, deepl())
        self.pack.outside = self.web
        self.pack.translator.pace = 0
        await self.bot.settings.save(self.guild.id, "translation", {"enabled": True})

    def said(self, ctx) -> str:
        return ctx.sent[-1]["content"]


class CurrencyCommandTest(Case):
    async def test_it_answers_with_both_lines_and_the_date(self):
        ctx = self.context(author=self.admin)
        await self.pack.currency.callback(self.pack, ctx, 100.0, "usd", "eur")
        self.assertEqual(self.said(ctx), "100.00 USD - US Dollar = **90.00** EUR - Euro (as of 2026-11-06)\n(1 USD - US Dollar ≈ 0.9 EUR - Euro)")
        self.assertFalse(ctx.sent[-1]["ephemeral"])
        self.assertEqual(ctx.sent[-1]["allowed_mentions"].to_dict()["parse"], [])

    async def test_a_name_typed_in_the_option_works_too(self):
        ctx = self.context(author=self.admin)
        await self.pack.currency.callback(self.pack, ctx, 2.0, "Euro", "cad")
        self.assertTrue(self.said(ctx).startswith("2.00 Euro (EUR - Euro) = **3.11** CAD - Canadian Dollar"), self.said(ctx))

    async def test_the_same_currency_on_both_sides_still_answers_when_asked_by_command(self):
        ctx = self.context(author=self.admin)
        await self.pack.currency.callback(self.pack, ctx, 5.0, "usd", "usd")
        self.assertTrue(self.said(ctx).startswith("5.00 USD - US Dollar = **5.00** USD - US Dollar"))

    async def test_a_tiny_result_keeps_its_digits(self):
        ctx = self.context(author=self.admin)
        await self.pack.currency.callback(self.pack, ctx, 1.0, "usd", "btc")
        self.assertIn("**0.0000100**", self.said(ctx))

    async def test_an_unknown_currency_is_named_with_the_remedy(self):
        ctx = self.context(author=self.admin)
        await self.pack.currency.callback(self.pack, ctx, 1.0, "usd", "zzz")
        self.assertEqual(ctx.fails, ["`zzz` isn't a currency I know. Pick one from the list as you type."])
        self.assertEqual(ctx.sent, [])

    async def test_the_same_unknown_code_on_both_sides_is_no_currency(self):
        # The old bot answered "5 LOL = 5 LOL" for any code repeated.
        ctx = self.context(author=self.admin)
        await self.pack.currency.callback(self.pack, ctx, 5.0, "lol", "lol")
        self.assertTrue(ctx.fails)

    async def test_with_the_rate_service_down_it_says_so(self):
        self.pack.outside = FakeOutside()
        ctx = self.context(author=self.admin)
        await self.pack.currency.callback(self.pack, ctx, 1.0, "usd", "eur")
        self.assertEqual(ctx.fails, ["The exchange rate service didn't answer. Try again in a few minutes."])

    async def test_someone_who_cant_post_here_sees_it_alone(self):
        ctx = self.context(author=self.member)
        await self.pack.currency.callback(self.pack, ctx, 1.0, "usd", "eur")
        self.assertTrue(ctx.sent[-1]["ephemeral"])

    async def test_the_list_offers_codes_by_their_names(self):
        choices = await self.pack._currency_choices(None, "dollar")
        self.assertEqual([(choice.name, choice.value) for choice in choices], [("CAD - Canadian Dollar", "cad"), ("USD - US Dollar", "usd")])
        self.assertEqual(len(await self.pack._currency_choices(None, "")), 7, "every code with a usable rate")


class RatesTest(Case):
    async def test_the_second_source_answers_when_the_first_is_down(self):
        web = FakeOutside()
        web.answer(f"{RATE_SOURCES[1]}/currencies/usd.json", RATES).answer(f"{RATE_SOURCES[1]}/currencies.json", NAMES)
        self.pack.outside = web
        await self.pack.ensure_rates()
        self.assertEqual(self.pack.rate("usd", "eur"), 0.9)
        self.assertEqual([call["url"] for call in web.calls][:2], [f"{RATE_SOURCES[0]}/currencies/usd.json", f"{RATE_SOURCES[1]}/currencies/usd.json"])

    async def test_rates_last_an_hour_and_names_are_fetched_once(self):
        await self.pack.ensure_rates()
        await self.pack.ensure_rates()
        self.assertEqual(len(self.web.called(f"{RATE_SOURCES[0]}/currencies/usd.json")), 1)
        self.pack._fetched_at -= 3601
        await self.pack.ensure_rates()
        self.assertEqual(len(self.web.called(f"{RATE_SOURCES[0]}/currencies/usd.json")), 2)
        self.assertEqual(len(self.web.called(f"{RATE_SOURCES[0]}/currencies.json")), 1)

    async def test_a_failed_fetch_keeps_the_rates_on_hand_and_waits_before_trying_again(self):
        await self.pack.ensure_rates()
        self.pack._fetched_at -= 3601
        down = FakeOutside()
        self.pack.outside = down
        await self.pack.ensure_rates()
        self.assertEqual(self.pack.rate("usd", "eur"), 0.9)
        tries = len(down.calls)
        await self.pack.ensure_rates()
        self.assertEqual(len(down.calls), tries, "a dead service isn't asked on every message")

    async def test_a_start_with_the_service_down_uses_the_last_rates_saved(self):
        await self.pack.ensure_rates()
        self.assertIsNotNone(await self.db.scalar("SELECT value FROM kv WHERE key = ?", (RATES_KEY,)))
        fresh = Translation(self.bot)
        fresh.outside = FakeOutside()
        await fresh.ensure_rates()
        self.assertEqual(fresh.rate("usd", "cad"), 1.4)
        self.assertEqual(fresh._rate_date, "2026-11-06")

    async def test_a_code_repeated_on_both_sides_needs_a_rate(self):
        await self.pack.ensure_rates()
        self.assertEqual(self.pack.rate("eur", "eur"), 1.0)
        self.assertIsNone(self.pack.rate("lol", "lol"), "the old bot answered 5 LOL = 5 LOL")

    async def test_a_date_that_isnt_one_shows_no_date(self):
        self.pack.outside = rates_site(FakeOutside(), {**RATES, "date": "<@&1> **today**"})
        await self.pack.ensure_rates()
        self.assertIsNone(self.pack._rate_date)
        self.assertEqual(self.pack._as_of(), "")

    def test_a_zero_or_broken_rate_is_left_out(self):
        self.assertEqual(clean_rates({"usd": 1, "doge": 0, "bad": "x", "neg": -1, "nan": float("nan"), "ok": 2.5}), {"usd": 1.0, "ok": 2.5})


class UnitsCommandTest(Case):
    async def test_it_answers_with_ten_places(self):
        ctx = self.context(author=self.admin)
        await self.pack.units.callback(self.pack, ctx, 5.0, "km", "mi")
        self.assertEqual(self.said(ctx), "5.00 km = **3.1068559612** mi")

    async def test_an_unknown_pair_lists_what_works(self):
        ctx = self.context(author=self.admin)
        await self.pack.units.callback(self.pack, ctx, 5.0, "km", "kg")
        self.assertEqual(ctx.fails, [convert.UNITS_HELP])

    async def test_the_list_offers_units_as_typed(self):
        choices = await self.pack._unit_choices(None, "/s")
        self.assertIn("m/s", [choice.value for choice in choices])
        self.assertLessEqual(len(await self.pack._unit_choices(None, "")), 25)


class TranslateCommandTest(Case):
    async def test_it_posts_the_translation_with_its_languages(self):
        ctx = self.context(author=self.admin)
        await self.pack.translate.callback(self.pack, ctx, "hola amigo", "english")
        self.assertEqual(self.said(ctx), "\U0001f310 **Spanish → English (US)**\n[en-US] hola amigo")
        self.assertFalse(ctx.sent[-1]["ephemeral"])

    async def test_an_unknown_language_is_refused(self):
        ctx = self.context(author=self.admin)
        await self.pack.translate.callback(self.pack, ctx, "hola", "klingon")
        self.assertIn("Unknown target language: `klingon`", ctx.fails[0])
        self.assertEqual(self.web.called(tr.FREE_URL), [])

    async def test_a_named_source_goes_along(self):
        await self.pack.translate.callback(self.pack, self.context(author=self.admin), "hola", "english", "spanish")
        self.assertEqual(json.loads(self.web.called(tr.FREE_URL)[0]["data"])["source_lang"], "es")

    async def test_the_sixth_in_thirty_seconds_waits(self):
        for _ in range(5):
            await self.pack.translate.callback(self.pack, self.context(author=self.admin), "hola", "english")
        ctx = self.context(author=self.admin)
        await self.pack.translate.callback(self.pack, ctx, "hola", "english")
        self.assertEqual(ctx.fails, ["You're translating too fast. Give it a few seconds."])

    async def test_a_service_error_is_said(self):
        self.web.answer(tr.FREE_URL, reply({}, 429))
        self.pack.translator.sleep = self._no_sleep
        ctx = self.context(author=self.admin)
        await self.pack.translate.callback(self.pack, ctx, "hola", "english")
        self.assertEqual(self.said(ctx), "\U0001f310 Translation is rate-limited right now. Try again in a moment.")

    async def _no_sleep(self, seconds):
        pass

    async def test_someone_who_cant_post_here_sees_it_alone(self):
        ctx = self.context(author=self.member)
        await self.pack.translate.callback(self.pack, ctx, "hola", "english")
        self.assertTrue(ctx.sent[-1]["ephemeral"])

    async def test_a_translation_too_long_for_one_message_goes_in_several(self):
        long_text = "Una frase bastante larga para la prueba. " * 95

        def stretch(call):
            body = json.loads(call["data"])
            return reply({"translations": [{"text": body["text"][0] * 3, "detected_source_language": "ES"}]})
        self.web.answer(tr.FREE_URL, stretch)
        ctx = self.context(author=self.admin)
        await self.pack.translate.callback(self.pack, ctx, long_text.strip(), "english")
        self.assertGreater(len(ctx.sent), 1)
        for sent in ctx.sent:
            self.assertLessEqual(sum(len(embed.description) + len(embed.title or "") for embed in sent["embeds"]), tr.EMBEDS_TOTAL)


class LanguageCommandsTest(Case):
    async def test_a_saved_language_is_this_servers(self):
        ctx = self.context(author=self.member)
        await self.pack.translate_language.callback(self.pack, ctx, language="brazilian")
        self.assertEqual(ctx.oks, ["Saved. One-tap translations in this server will go to **Portuguese (Brazil)**."])
        self.assertEqual(await self.pack.saved_language(self.guild.id, self.member.id), "PT-BR")
        self.assertIsNone(await self.pack.saved_language(424242, self.member.id))
        self.assertEqual(await self.pack.default_target(self.guild.id, self.member.id, "fr"), "PT-BR")

    async def test_clearing_falls_back_to_the_discord_language_then_english(self):
        await self.pack.translate_language.callback(self.pack, self.context(author=self.member), language="german")
        ctx = self.context(author=self.member)
        await self.pack.translate_language.callback(self.pack, ctx, language="auto")
        self.assertIn("Cleared.", ctx.oks[0])
        self.assertEqual(await self.pack.default_target(self.guild.id, self.member.id, "fr"), "FR")
        self.assertEqual(await self.pack.default_target(self.guild.id, self.member.id, "hi"), "EN-US")
        self.assertEqual(await self.pack.default_target(self.guild.id, self.member.id), "EN-US")

    async def test_an_unknown_language_is_refused(self):
        ctx = self.context(author=self.member)
        await self.pack.translate_language.callback(self.pack, ctx, language="klingon")
        self.assertEqual(ctx.fails, ["Unknown language: `klingon`. Try Spanish, Japanese or French, or a code like ES."])

    async def test_a_language_written_by_hand_into_a_backup_is_passed_over(self):
        await self.db.run("INSERT INTO translate_languages (guild_id, user_id, language, updated_at) VALUES (?, ?, 'XX', 1)", (self.guild.id, self.member.id))
        self.assertEqual(await self.pack.default_target(self.guild.id, self.member.id), "EN-US")

    async def test_the_list_offers_auto_first(self):
        choices = await self.pack._saved_language_choices(None, "")
        self.assertEqual((choices[0].name, choices[0].value), ("Auto (use my Discord language)", "reset"))
        self.assertEqual(len(choices), 25)

    async def test_the_delete_time_is_clamped_and_zero_is_kept(self):
        ctx = self.context(author=self.admin)
        await self.pack.translate_autodelete.callback(self.pack, ctx, 99999)
        self.assertEqual(self.settings("translation")["flag_delete_seconds"], 3600)
        self.assertIn("**3600s**", ctx.oks[0])
        await self.pack.translate_autodelete.callback(self.pack, ctx, 0)
        self.assertEqual(self.settings("translation")["flag_delete_seconds"], 0)
        self.assertIn("auto-delete off", ctx.oks[1])
        self.assertEqual(self.pack.translate_autodelete.callback.__required_permissions__, ("administrator",))
        self.assertEqual(Translation.schema.fields["flag_delete_seconds"].default, 120)

    async def test_who_can_run_what(self):
        for command in (self.pack.currency, self.pack.units, self.pack.translate, self.pack.translate_language):
            self.assertFalse(hasattr(command.callback, "__required_permissions__"), command.name)
        self.assertEqual(self.pack.export_emojis.callback.__required_permissions__, ("administrator",))


class EmojiFileTest(unittest.TestCase):
    def test_the_bytes_decide_the_extension(self):
        self.assertEqual(emoji_file_type(b"GIF89a....", True), "gif")
        self.assertEqual(emoji_file_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ", True), "webp")
        self.assertEqual(emoji_file_type(b"\x89PNG\r\n", False), "png")
        self.assertEqual(emoji_file_type(b"unknown", True), "gif")
        self.assertEqual(emoji_file_type(b"unknown", False), "png")


class SettingsPageTest(Case):
    async def test_the_page_saves_each_switch_and_hides_the_token(self):
        client = await self.client()
        channel = FakeChannel(self.guild, "quiet")
        form = {"answer_currency": False, "ignored_channels": [str(channel.id)], "flag_delete_seconds": 30, "deepl_token": "secret-token"}
        response = await client.put(self.path("/settings/translation"), json=form)
        self.assertEqual(response.status, 200, await response.text())
        saved = await response.json()
        self.assertEqual(saved["deepl_token"], "********")
        self.assertEqual(self.settings("translation")["deepl_token"], "secret-token")
        self.assertEqual(self.settings("translation")["ignored_channels"], [channel.id])
        response = await client.put(self.path("/settings/translation"), json={"flag_delete_seconds": 3601})
        self.assertEqual(response.status, 400)
