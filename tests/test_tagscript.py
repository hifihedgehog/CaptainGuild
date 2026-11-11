"""The template language: what each block gives, that members' text stays data, and that no template can hang or crash anything."""
import datetime
import random
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from captainguild.core import messages
from captainguild.core.durations import ordinal
from captainguild.core.invitewatch import Invite
from captainguild.core.tagscript import (
    LimitReached, MAX_BLOCKS, MAX_COMMANDS, MAX_REACTIONS, MAX_VALUE, Interpreter, Scope, clean_template_message, fit_payload, render_payload, render_strict, render_text, split_block,
)
from captainguild.core.tagscript import numerals, objects
from captainguild.core.tagscript.clock import describe_gap, format_time, parse_when
from captainguild.core.tagscript.mathexpr import MAX_DEPTH, evaluate
from captainguild.core.tagscript import clock as clock_module
from captainguild.core.tagscript import engine as engine_module
from captainguild.core.tagscript import text as text_module
from captainguild.core.tagscript.text import plain, written_by_template
from captainguild.core.validate import Invalid
from tests.fakes import FakeChannel, FakeGuild, FakeMember

UTC = datetime.timezone.utc
SENTENCE = "Sam likes Subway. Today he got a Spicy Italian with - double pepperoni, double salami, provolone, and tomatoes."


def at(*parts: int) -> float:
    return datetime.datetime(*parts, tzinfo=UTC).timestamp()


def text(template: str, **scope) -> str:
    return render_text(template, Scope(**scope))


def run(template: str, **scope) -> Interpreter:
    interpreter = Interpreter(Scope(**scope))
    interpreter.output = interpreter.render(template)
    return interpreter


def arabic(grouped: str) -> str:
    """A number written with commas, as the arabic format writes it: Arabic-Indic digits (U+0660 on) and the Arabic thousands separator (U+066C)."""
    return "".join(chr(0x066C) if character == "," else chr(0x0660 + int(character)) for character in grouped)


class VariablesTest(unittest.TestCase):
    def word(self, template: str) -> str:
        return text(template, args=SENTENCE)

    def test_words_by_position(self):
        self.assertEqual(self.word("{args(1)}"), "Sam")
        self.assertEqual(self.word("{args(2)}"), "likes")
        self.assertEqual(self.word("{args(3)}"), "Subway.")
        self.assertEqual(self.word("{1} {2}"), "Sam likes")

    def test_counting_back_from_the_end(self):
        self.assertEqual(self.word("{args(0)}"), "tomatoes.")
        self.assertEqual(self.word("{args(-1)}"), "and")
        self.assertEqual(self.word("{args(-2)}"), "provolone,")

    def test_spans(self):
        self.assertEqual(self.word("{args(+9)}"), "Sam likes Subway. Today he got a Spicy Italian")
        self.assertEqual(self.word("{args(-11+)}"), "a Spicy Italian with - double pepperoni, double salami, provolone, and tomatoes.")

    def test_a_chosen_delimiter(self):
        self.assertEqual(self.word("{args(2):.}"), "Today he got a Spicy Italian with - double pepperoni, double salami, provolone, and tomatoes")
        toppings = "{=(toppings):{args(2):-}}"
        self.assertEqual(self.word(toppings + "{toppings(1):,}"), "double pepperoni")
        self.assertEqual(self.word(toppings + "{toppings(2):,}"), "double salami")
        self.assertEqual(self.word(toppings + "{toppings(0):,}"), "and tomatoes.")

    def test_out_of_bounds(self):
        self.assertEqual(self.word("{args(19)}"), SENTENCE)
        self.assertEqual(self.word("{args(3+):.}"), "")

    def test_a_useless_index_or_delimiter_gives_the_whole_value(self):
        self.assertEqual(self.word("{args(nope)}"), SENTENCE)
        self.assertEqual(self.word("{args(1):}"), SENTENCE)

    def test_every_alias_assigns(self):
        for name in ("=", "assign", "let", "var"):
            self.assertEqual(text("{%s(x):hello}{x}" % name), "hello")

    def test_nested_names_and_built_names(self):
        self.assertEqual(text("{=(msg1):Hello World}{=(msg2):How are you today?}{msg{1}}", args="2"), "How are you today?")
        self.assertEqual(text("{assign(start):thisis}{assign(thisistorb):FOUND} Okay, {{start}torb}"), "Okay, FOUND")

    def test_args_and_message_are_separate_and_digits_follow_message(self):
        self.assertEqual(text("{=(args):other}{args} {message}", args="kept"), "other kept")
        self.assertEqual(text("{=(message):a b c}{2} {args}", args="kept"), "b kept")

    def test_a_variable_can_replace_a_default(self):
        self.assertEqual(text("{=(uses):many}{uses}", uses=3), "many")

    def test_uses_unix_and_extra(self):
        self.assertEqual(text("{uses}", uses=7), "7")
        self.assertEqual(text("{unix}", now=at(2020, 1, 1)), str(int(at(2020, 1, 1))))
        self.assertEqual(text("{reason}", extra={"reason": "spam"}), "spam")
        self.assertEqual(text("{reason}{none}", extra={"reason": 5, "none": None}), "5")

    def test_unknown_blocks_stay_as_written(self):
        self.assertEqual(text("{nothing} {nothing(1)} {nothing:x} {} {"), "{nothing} {nothing(1)} {nothing:x} {} {")

    def test_an_empty_named_variable_answers_empty_braces(self):
        self.assertEqual(text("{=():}{}"), "")

    def test_a_block_name_wins_over_a_variable_unless_it_can_not_run(self):
        self.assertEqual(text("{=(math):seven}{math:1+1} {math}"), "2.0 seven")

    def test_block_names_ignore_case_and_variable_names_do_not(self):
        self.assertEqual(text("{UPPER:a}{Upper:b}"), "AB")
        self.assertEqual(text("{=(Name):x}{name}{Name}"), "{name}x")

    def test_output_is_trimmed_of_spaces_and_newlines(self):
        self.assertEqual(text("{=(a):1}\n\n  hello {a}\n \n"), "hello 1")


class ShapeTest(unittest.TestCase):
    def test_splitting_a_block(self):
        self.assertEqual(split_block("{a}"), ("a", None, None))
        self.assertEqual(split_block("{a:b:c}"), ("a", None, "b:c"))
        self.assertEqual(split_block("{a(b):c}"), ("a", "b", "c"))
        self.assertEqual(split_block("{a(b(c)):d}"), ("a", "b(c)", "d"))
        self.assertEqual(split_block("{a(b)x:y}"), ("a", "b", None))
        self.assertEqual(split_block("{a(b:c}"), ("a(b", None, "c"))
        self.assertEqual(split_block("{}"), ("", None, None))

    def test_a_long_parameter_is_still_a_parameter(self):
        self.assertEqual(text("{if(%s==%s):same|different}" % ("x" * 500, "x" * 500)), "same")

    def test_stray_braces_are_text(self):
        self.assertEqual(text("} {a {upper:b} c"), "} {a B c")
        self.assertEqual(text("{{upper:b}"), "{B")
        self.assertEqual(text("{upper:b}}"), "B}")

    def test_blocks_run_inside_out_and_left_to_right(self):
        self.assertEqual(text("{=(a):1}{=(a):{a}2}{=(a):{a}3}{a}"), "123")
        self.assertEqual(text("{upper:{lower:ABC}d}"), "ABCD")


class ControlTest(unittest.TestCase):
    def test_comparisons(self):
        for equation, expected in [("cat==dog", False), ("5==5", True), ("tree!=car", True), ("10!=10", False), ("15>20", False), ("17>=17", True), ("1<=1", True), ("25<5", False)]:
            self.assertEqual(text("{if(%s):yes|no}" % equation), "yes" if expected else "no", equation)

    def test_spaces_around_a_comparison_do_not_count(self):
        self.assertEqual(text("{if( a == a ):yes|no}"), "yes")

    def test_if_without_else_is_nothing(self):
        self.assertEqual(text("[{if(1==2):yes}]"), "[]")

    def test_if_that_can_not_compare_stays_as_written(self):
        self.assertEqual(text("{if(a>b):yes|no}"), "{if(a>b):yes|no}")
        self.assertEqual(text("{if(nothing here):yes|no}"), "{if(nothing here):yes|no}")

    def test_any_and_all(self):
        self.assertEqual(text("{any(1==2|2==2):yes|no}"), "yes")
        self.assertEqual(text("{or(1==2|2==3):yes|no}"), "no")
        self.assertEqual(text("{all(1==1|2==2):yes|no}"), "yes")
        self.assertEqual(text("{and(1==1|2==3):yes|no}"), "no")
        self.assertEqual(text("{all({1}>0|{1}<=100):You ate {args} tacos.|You must input a number between 1 and 100}", args="5"), "You ate 5 tacos.")
        self.assertEqual(text("{all({1}>0|{1}<=100):You ate {args} tacos.|You must input a number between 1 and 100}", args="many"), "You must input a number between 1 and 100")

    def test_more_than_one_pipe_means_all_of_it_is_the_then_part(self):
        self.assertEqual(text("{if(1==1):a|b|c}"), "a|b|c")
        self.assertEqual(text("[{if(1==2):a|b|c}]"), "[]")

    def test_break_replaces_the_output(self):
        for name in ("break", "short", "shortcircuit"):
            self.assertEqual(text("{%s(1==1):Only this.} All of this is gone." % name), "Only this.")
        self.assertEqual(text("{break(1==2):Only this.} This stays."), "This stays.")
        self.assertEqual(text("{break({1}==):You did not provide the proper input.}rest"), "You did not provide the proper input.")

    def test_a_block_after_a_break_still_runs(self):
        interpreter = run("{break(1==1):out}{cmd:ping}")
        self.assertEqual(interpreter.output, "out")
        self.assertEqual(interpreter.actions.commands, [("ping", False)])

    def test_stop_halts_at_once(self):
        interpreter = run("before {stop(1==1):end} after {cmd:ping}")
        self.assertEqual(interpreter.output, "before end")
        self.assertEqual(interpreter.actions.commands, [])
        self.assertEqual(text("a{halt(1==2):x}b"), "ab")

    def test_every_complete_block_runs_even_in_a_branch_not_taken(self):
        self.assertEqual(text("{if(1==2):{=(x):set}}{x}"), "set")
        interpreter = run("{if(1==2):{cmd:ping}}")
        self.assertEqual(interpreter.actions.commands, [("ping", False)])

    def test_the_double_bracket_way_to_make_a_block_conditional(self):
        taken = run("{=():}{{if(1==1):cmd:role add 5 Verified}}")
        self.assertEqual((taken.output, taken.actions.commands), ("", [("role add 5 Verified", False)]))
        skipped = run("{=():}{{if(1==2):cmd:role add 5 Verified}}")
        self.assertEqual((skipped.output, skipped.actions.commands), ("", []))

    def test_the_other_ways_to_make_a_command_conditional(self):
        secret = "{=(passphrase):open}"
        right = run(secret + "{cmd:{if({args}=={passphrase}):role add 1 Verified|echo That is not the passphrase}}", args="open")
        self.assertEqual(right.actions.commands, [("role add 1 Verified", False)])
        wrong = run(secret + "{cmd:role add 1 {if({args}=={passphrase}):Verified|UnVerified}}", args="closed")
        self.assertEqual(wrong.actions.commands, [("role add 1 UnVerified", False)])

    def test_data_tables_through_double_brackets(self):
        template = (
            "{assign(xmas):Christmas|-12-25|tree}{assign(vday):Valentine's Day|-02-14|heart}"
            "{any({args}==xmas|{args}==vday):Holiday: {{args}(1):|}\nDate: {{args}(2):|}\nEmoji: {{args}(3):|}}"
        )
        self.assertEqual(text(template, args="vday"), "Holiday: Valentine's Day\nDate: -02-14\nEmoji: heart")
        self.assertEqual(text(template, args="xmas"), "Holiday: Christmas\nDate: -12-25\nEmoji: tree")
        self.assertEqual(text(template, args="easter"), "")


class ListsTest(unittest.TestCase):
    def test_list(self):
        self.assertEqual(text("{list(3):Apples~Bananas~Pears~Grapes~Oranges}"), "Grapes")
        self.assertEqual(text("{list(0):a,b}"), "a")
        self.assertEqual(text("{list(-1):a,b}"), "b")
        self.assertEqual(text("[{list(9):a,b}]"), "[]")
        self.assertIn("number", text("{list(x):a,b}"))

    def test_cycle(self):
        self.assertEqual(text("{cycle(11):Apples,Bananas,Pears,Grapes,Oranges}"), "Bananas")
        self.assertEqual(text("{cycle(-1):a,b,c}"), "c")
        self.assertIn("number", text("{cycle(x):a,b}"))

    def test_index(self):
        self.assertEqual(text("{index(bread):Which do you like more, bread or chocolate? I like bread more}"), "5")
        self.assertEqual(text("{index(cake):a b}"), "-1")

    def test_in_and_contains(self):
        self.assertEqual(text("{in(spam):Most server rules prohibit spamming.}"), "true")
        self.assertEqual(text("{in(spicy italian):Today Sam had a spicy italian sub.}"), "true")
        self.assertEqual(text("{in(sam):Sam is awesome}"), "false")
        self.assertEqual(text("{contains(violet):blue black green gray violet red yellow}"), "true")
        self.assertEqual(text("{contains(maple):pine fir aspen oak dogwood}"), "false")
        self.assertEqual(text("{contains(viol):violet}"), "false")


class TextBlocksTest(unittest.TestCase):
    def test_ord(self):
        for number, expected in [(101, "101st"), (22, "22nd"), (3, "3rd"), (456, "456th"), (8102, "8,102nd"), (11, "11th"), (112, "112th")]:
            self.assertEqual(text("{ord:%d}" % number), expected)
        self.assertEqual(text("{ord:{m:1+1}}"), "2nd")
        self.assertEqual(text("{ord:word}"), "word")

    def test_case(self):
        self.assertEqual(text("{lower:Whoozard is a Wizard}"), "whoozard is a wizard")
        self.assertEqual(text("{upper:the best bot}"), "THE BEST BOT")

    def test_join(self):
        self.assertEqual(text("{join(_):hello friends}"), "hello_friends")
        self.assertEqual(text("{join():an example sentence}"), "anexamplesentence")
        self.assertEqual(text("{join:no parameter}"), "{join:no parameter}")

    def test_replace(self):
        self.assertEqual(text("{replace(oo,ee):Goose Tooth Moose}"), "Geese Teeth Meese")
        self.assertEqual(text("/{replace(, ):Sam-Bot}/"), "/ S a m - B o t /")
        self.assertEqual(text("{replace(o,):foo}"), "f")
        self.assertEqual(text("{replace(x):no comma}"), "{replace(x):no comma}")

    def test_urlencode(self):
        self.assertEqual(text("{urlencode:Hey there, how are you?}"), "Hey%20there%2C%20how%20are%20you%3F")
        self.assertEqual(text("{urlencode(+):Hey there, how are you?}"), "Hey+there%2C+how+are+you%3F")
        self.assertEqual(text("https://www.google.com/search?q={urlencode(+):Sam-bot's dashboard}"), "https://www.google.com/search?q=Sam-bot%27s+dashboard")

    def test_substring(self):
        self.assertEqual(text("{substr(1-3):abcde}"), "bc")
        self.assertEqual(text("{substring(2):abcde}"), "cde")
        self.assertEqual(text("{substr(x):abcde}"), "{substr(x):abcde}")


class MathTest(unittest.TestCase):
    def test_documented_results(self):
        self.assertEqual(text("{math:cos(pi)}"), "-1.0")
        self.assertEqual(text("{m:round(7.8)+trunc(8.9)}"), "16")

    def test_every_alias_and_every_operation(self):
        self.assertEqual(text("{math:8+9}"), "17.0")
        self.assertEqual(text("{calc:7-4}"), "3.0")
        self.assertEqual(text("{+:8/2}"), "4.0")
        self.assertEqual(text("{m:5*6}"), "30.0")
        self.assertEqual(text("{m:7%4}"), "3.0")
        self.assertEqual(text("{m:2^10}"), "1024.0")

    def test_functions(self):
        for expression, expected in [
            ("abs(-3)", "3.0"), ("round(2.5)", "2"), ("trunc(-2.9)", "-2"), ("exp(0)", "1.0"), ("sgn(5)", "1"), ("sgn(-5)", "-1"), ("sgn(0)", "0"),
            ("log(1000)", "3.0"), ("ln(e)", "1.0"), ("log2(8)", "3.0"), ("sin(0)", "0.0"), ("tan(0)", "0.0"), ("cosh(0)", "1.0"), ("sinh(0)", "0.0"), ("tanh(0)", "0.0"),
            ("PI", "3.141592653589793"), ("pi", "3.141592653589793"), ("E", "2.718281828459045"), ("e", "2.718281828459045"),
        ]:
            self.assertEqual(evaluate(expression), expected, expression)

    def test_order_of_operations(self):
        self.assertEqual(evaluate("1+2*3"), "7.0")
        self.assertEqual(evaluate("(1+2)*3"), "9.0")
        self.assertEqual(evaluate("2^3^2"), "512.0")
        self.assertEqual(evaluate("2*-3"), "-6.0")
        self.assertEqual(evaluate("-2^2"), "4.0")
        self.assertEqual(evaluate(" 1 + 1 "), "2.0")

    def test_whole_numbers_stay_whole_until_divided(self):
        self.assertEqual(evaluate("trunc(7)+trunc(8)"), "15")
        self.assertEqual(evaluate("trunc(7)/trunc(2)"), "3.5")

    def test_what_can_not_be_worked_out_stays_as_written(self):
        for expression in ("100**100**100", "0/0", "5%0", "1+", "()", "foo(1)", "foo", "2e", "1 2", "(-8)^0.5", "ln(0)", "2^99999"):
            self.assertIsNone(evaluate(expression), expression)
            self.assertEqual(text("{m:%s}" % expression), "{m:%s}" % expression)

    def test_a_tower_of_whole_powers_does_not_hang(self):
        started = time.monotonic()
        self.assertIsNone(evaluate("trunc(9)^trunc(9)^trunc(9)"))
        self.assertIsNone(evaluate("*".join(["trunc(1e308)"] * 30)))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_deep_nesting_is_refused_not_a_stack_overflow(self):
        self.assertIsNone(evaluate("(" * 4000 + "1" + ")" * 4000))
        self.assertIsNone(evaluate("1" + "^1" * 2400))
        self.assertIsNone(evaluate("1+" * 3000 + "1"))


class RandomTest(unittest.TestCase):
    def test_commas_and_tildes(self):
        seen = {text("{random:a,b}") for _ in range(60)}
        self.assertEqual(seen, {"a", "b"})
        seen = {text("{rand:Hello, how are you?~Howdy}") for _ in range(60)}
        self.assertEqual(seen, {"Hello, how are you?", "Howdy"})
        seen = {text("{#:{#:x,y} cakes~none}") for _ in range(80)}
        self.assertEqual(seen, {"x cakes", "y cakes", "none"})

    def test_a_list_can_come_from_a_variable_or_a_member(self):
        self.assertIn(text("{=(items):1,2,3}{random:{items}}"), {"1", "2", "3"})
        self.assertIn(text("{random:{args}}", args="red,green"), {"red", "green"})

    def test_weights_count_like_repeats(self):
        rolls = [text("{random:99|lost,won}") for _ in range(400)]
        self.assertGreater(rolls.count("lost"), 360)
        self.assertEqual(set(rolls) - {"lost", "won"}, set())
        self.assertEqual(text("{random(s):0|never,1|always}"), "always")
        for seed in range(50):
            self.assertEqual(text("{random(%d):4|a,2|b}" % seed), text("{random(%d):a,a,a,a,b,b}" % seed))

    def test_a_seed_locks_the_choice(self):
        for seed in ("one", "two", "1234"):
            first = text("{random(%s):a,b,c,d,e,f}" % seed)
            self.assertTrue(all(text("{random(%s):a,b,c,d,e,f}" % seed) == first for _ in range(20)))
        self.assertEqual(text("{#(9):1,2,3} {#(9):one,two,three}").split()[0], {"one": "1", "two": "2", "three": "3"}[text("{#(9):1,2,3} {#(9):one,two,three}").split()[1]])
        self.assertEqual(text("{random(same):a,b,c}"), random.Random("same").choice(["a", "b", "c"]))

    def test_range(self):
        self.assertTrue(all(1 <= int(text("{range:1-6}")) <= 6 for _ in range(50)))
        self.assertEqual({text("{range:1-2}") for _ in range(60)}, {"1", "2"})
        self.assertEqual(text("{range(s):1-100}"), text("{range(s):1-100}"))
        self.assertEqual(text("{range(s):1-100}"), str(random.Random("s").randint(1, 100)))
        self.assertEqual(text("{range:nope}"), "{range:nope}")

    def test_rangef_has_one_decimal(self):
        for _ in range(50):
            value = text("{rangef:8-9}")
            self.assertTrue(8.0 <= float(value) <= 9.0)
            self.assertRegex(value, r"^\d\.\d$")

    def test_fifty_fifty(self):
        for name in ("5050", "50", "?"):
            self.assertEqual({text("Hi{%s: there}" % name) for _ in range(60)}, {"Hi", "Hi there"})


class TimeTest(unittest.TestCase):
    NOW = at(2019, 11, 26, 3, 0, 0)

    def test_strf_with_a_date_in_either_form_or_unix_time(self):
        self.assertEqual(text("{strf(1999-12-31 23.59.59):%A}"), "Friday")
        self.assertEqual(text("{strf(1999-12-31 23:59:59):%A}"), "Friday")
        self.assertEqual(text("{strf(946684799):%Y-%m-%d %H:%M:%S}"), "1999-12-31 23:59:59")

    def test_strf_defaults_to_now(self):
        self.assertEqual(text("{strf:%Y}", now=self.NOW), "2019")
        self.assertEqual(text("{strf():%Y}", now=self.NOW), "2019")

    def test_bad_dates_stay_as_written(self):
        self.assertEqual(text("{strf(someday):%Y}"), "{strf(someday):%Y}")
        self.assertEqual(text("{strf(1999-13-45 00:00:00):%Y}"), "{strf(1999-13-45 00:00:00):%Y}")
        self.assertEqual(text("{strf(99999999999999999999):%Y}"), "{strf(99999999999999999999):%Y}")
        self.assertEqual(text("{strf}"), "{strf}")

    def test_format_codes_the_docs_name(self):
        moment = datetime.datetime(2015, 12, 24, 14, 5, 9, tzinfo=UTC)
        for code, expected in [
            ("%-I:%M %p", "2:05 PM"), ("%x", "2015/12/24"), ("%F %T", "2015-12-24 14:05:09"), ("%FT%T", "2015-12-24T14:05:09"), ("%Y-%m-%d %H:%M:%S", "2015-12-24 14:05:09"),
            ("%s", str(int(moment.timestamp()))), ("%A %a %B %b %h", "Thursday Thu December Dec Dec"), ("%j", "358"), ("%y %C", "15 20"), ("%D", "12/24/15"), ("%R", "14:05"),
            ("%r", "02:05:09 PM"), ("%e|%-d|%_d|%k|%l|%P", "24|24|24|14| 2|pm"), ("%u %w", "4 4"), ("%U %W %V %G %g", "51 51 52 2015 15"), ("%z %Z %:z", "+0000 UTC +00:00"),
            ("%c", "Thu Dec 24 14:05:09 2015"), ("%X", "14:05:09"), ("%f", "000000"), ("%^a %#B", "THU DECEMBER"), ("%%", "%"), ("%n%t", "\n\t"), ("%Q", "%Q"), ("100%", "100%"), ("%05d|%3e", "00024| 24"),
        ]:
            self.assertEqual(format_time(moment, code), expected, code)

    def test_single_digit_padding(self):
        moment = datetime.datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC)
        self.assertEqual(format_time(moment, "%d %-d %e %_d %m %-m %H %-H %k"), "04 4  4  4 03 3 05 5  5")

    def test_parsing_dates(self):
        expected = datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
        for form in ("2020-01-02 03:04:05", "2020-01-02 03.04.05", "2020-01-02T03:04:05.000Z", "2020-01-02T03:04:05Z", str(int(expected.timestamp()))):
            self.assertEqual(parse_when(form), expected, form)
        self.assertEqual(parse_when("2020-01-02"), datetime.datetime(2020, 1, 2, tzinfo=UTC))
        self.assertIsNone(parse_when("soon"))

    def test_timedelta_examples(self):
        self.assertEqual(text("{td:2020-01-01 00:00:00}", now=self.NOW), "1 month, 5 days and 21 hours")
        self.assertEqual(text("{td({m:trunc({unix}-3600)}):{strf:%Y-%m-%d %H:%M:%S}}", now=self.NOW), "1 hour")

    def test_timedelta_in_calendar_terms(self):
        base = datetime.datetime(2024, 1, 31, tzinfo=UTC)
        self.assertEqual(describe_gap(base, datetime.datetime(2024, 2, 29, tzinfo=UTC)), "1 month")
        self.assertEqual(describe_gap(base, base), "now")
        self.assertEqual(describe_gap(base, base + datetime.timedelta(days=9, seconds=1)), "1 week, 2 days and 1 second")
        self.assertEqual(describe_gap(datetime.datetime(2022, 1, 1, tzinfo=UTC), datetime.datetime(2025, 3, 1, tzinfo=UTC)), "3 years and 2 months")
        self.assertEqual(describe_gap(datetime.datetime(2025, 3, 1, tzinfo=UTC), datetime.datetime(2022, 1, 1, tzinfo=UTC)), "3 years and 2 months")

    def test_a_template_reads_one_clock_through_a_whole_run(self):
        first, second = text("{strf:%s}{strf:%s}", now=self.NOW)[:10], text("{strf:%s}{strf:%s}", now=self.NOW)[10:]
        self.assertEqual(first, second)


class DiscordObjectsTest(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild("Harbor")
        self.mod = self.guild.role("Moderator", 5, "manage_messages", color=0x5989AB)
        self.member_role = self.guild.role("Member", 2)
        self.me = FakeMember(self.guild, "bot", bot=True)
        self.guild.me = self.me
        self.owner = FakeMember(self.guild, "owner")
        self.guild.owner_id = self.owner.id
        self.user = FakeMember(self.guild, "ada", self.member_role, self.mod)
        self.user.nick = "Ada L"
        self.other = FakeMember(self.guild, "grace")
        self.channel = FakeChannel(self.guild, "general")
        self.channel.topic = "Say hi"
        self.scope = Scope(guild=self.guild, member=self.user, channel=self.channel)

    def read(self, template: str, **changes) -> str:
        return render_text(template, Scope(guild=self.guild, member=self.user, channel=self.channel, **changes))

    def test_user_properties(self):
        created = self.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        joined = self.user.joined_at.strftime("%Y-%m-%d %H:%M:%S")
        for prop, expected in [
            ("avatar", "https://cdn.example/avatar.png"), ("icon", "https://cdn.example/avatar.png"), ("id", str(self.user.id)), ("mention", f"<@{self.user.id}>"), ("created_at", created),
            ("joined_at", joined), ("color", "#5989ab"), ("name", "ada"), ("proper", "ada"), ("position", "5"),
            ("roleids", f"{self.guild.default_role.id} {self.member_role.id} {self.mod.id}"),
        ]:
            self.assertEqual(self.read("{user(%s)}" % prop), expected, prop)

    def test_plain_names(self):
        self.assertEqual(self.read("{user}|{target}|{server}|{channel}|{mention}"), f"Ada L|Ada L|Harbor|general|<@{self.user.id}>")

    def test_target_is_who_was_mentioned(self):
        self.assertEqual(self.read("{target}|{target(id)}", target=self.other), f"grace|{self.other.id}")

    def test_server_properties(self):
        for prop, expected in [
            ("id", str(self.guild.id)), ("owner", "owner"), ("members", "4"), ("bots", "1"), ("humans", "3"), ("roles", "3"), ("channels", "1"), ("icon", ""),
            ("created_at", self.guild.created_at.strftime("%Y-%m-%d %H:%M:%S")),
        ]:
            self.assertEqual(self.read("{server(%s)}" % prop), expected, prop)

    def test_server_random_members(self):
        people = {str(m) for m in self.guild.members}
        for prop in ("random", "randomonline", "randomoffline"):
            self.assertIn(self.read("{server(%s)}" % prop), people)

    def test_channel_properties(self):
        for prop, expected in [("id", str(self.channel.id)), ("topic", "Say hi"), ("slowmode", "0"), ("position", "0"), ("mention", f"<#{self.channel.id}>")]:
            self.assertEqual(self.read("{channel(%s)}" % prop), expected, prop)

    def test_role_properties(self):
        scope = {"role": self.mod, "role_added": True}
        for prop, expected in [("mention", f"<@&{self.mod.id}>"), ("position", "5"), ("id", str(self.mod.id)), ("name", "Moderator"), ("is_added", "true"), ("has_elevated_perms", "true")]:
            self.assertEqual(self.read("{role(%s)}" % prop, **scope), expected, prop)
        self.assertEqual(self.read("{role}", **scope), "Moderator")
        self.assertEqual(self.read("{role(is_added)}", role=self.member_role, role_added=False), "false")
        self.assertEqual(self.read("{role(has_elevated_perms)}", role=self.member_role, role_added=False), "false")

    def test_unknown_property_stays_as_written(self):
        self.assertEqual(self.read("{user(shoe)}"), "{user(shoe)}")

    def test_a_number_reads_a_word_of_the_name(self):
        self.assertEqual(self.read("{user(2)}"), "L")

    def test_properties_as_a_condition(self):
        template = "{if({contains(%d):{user(roleids)}}==true):has it|lacks it}"
        self.assertEqual(self.read(template % self.mod.id), "has it")
        self.assertEqual(self.read(template % self.me.id), "lacks it")
        self.assertEqual(self.read("{if({user(id)}=={target(id)}):No target mentioned.|{user} says {target} is a wuss!}"), "No target mentioned.")
        self.assertEqual(self.read("{if({user(id)}=={target(id)}):No target mentioned.|{user} says {target} is a wuss!}", target=self.other), "Ada L says grace is a wuss!")

    def test_nothing_in_scope_leaves_names_as_written(self):
        self.assertEqual(text("{user} {target} {server} {channel} {mention} {role}"), "{user} {target} {server} {channel} {mention} {role}")

    def test_a_half_gone_discord_object_gives_empty_text(self):
        class Gone:
            id = 1

            @property
            def display_name(self):
                raise RuntimeError("gone")

        self.assertEqual(text("[{user}][{user(id)}][{user(avatar)}][{user(joined_at)}]", member=Gone()), "[][1][][]")

    def test_a_user_who_left_has_no_joined_date(self):
        left = SimpleNamespace(id=1, name="gone", display_name="gone", mention="<@1>", joined_at=None, created_at=None)
        self.assertEqual(text("[{user(joined_at)}][{user(created_at)}][{user(position)}]", member=left), "[][][0]")


class MetaBlocksTest(unittest.TestCase):
    def test_they_leave_no_text_and_record_what_they_ask(self):
        interpreter = run("a{delete}b{silent}c{override}d{dm}e")
        self.assertEqual(interpreter.output, "abcde")
        actions = interpreter.actions
        self.assertTrue(actions.delete and actions.silent and actions.override)
        self.assertEqual(actions.destination, "dm")

    def test_every_alias(self):
        self.assertTrue(run("{del}").actions.delete)
        self.assertTrue(run("{silence}").actions.silent)
        for name in ("command", "cmd", "c"):
            self.assertEqual(run("{%s:ping now}" % name).actions.commands, [("ping now", False)])
        self.assertEqual(run("{cmdAdmin:ping}").actions.commands, [("ping", True)])

    def test_the_last_destination_wins(self):
        self.assertEqual(run("{dm}{redirect:logs}").actions.channel_text, "logs")
        self.assertEqual(run("{redirect:logs}{dm}").actions.destination, "dm")

    def test_require_and_blacklist_keep_their_message_and_entries(self):
        actions = run("{require(you're not cool enough):Cool kids, <#5>}{blacklist:Staff}").actions
        self.assertEqual(actions.requires, [("you're not cool enough", ["Cool kids", "<#5>"])])
        self.assertEqual(actions.blacklists, [(None, ["Staff"])])

    def test_react_splits_on_spaces_and_stops_at_the_limit(self):
        actions = run("{react: :laughing: <:x:1>}{reactu:a b}").actions
        self.assertEqual(actions.react, [":laughing:", "<:x:1>"])
        self.assertEqual(actions.react_up, ["a", "b"])
        self.assertEqual(len(run("{react:1 2 3 4 5 6 7 8}").actions.react), MAX_REACTIONS)

    def test_command_limit(self):
        self.assertEqual(len(run("{cmd:a}" * (MAX_COMMANDS + 3)).actions.commands), MAX_COMMANDS)

    def test_an_empty_command_is_nothing(self):
        self.assertEqual(run("{c:}").actions.commands, [])

    def test_render_text_skips_them(self):
        self.assertEqual(text("Hi{delete}{cmd:ban me}{react:x}{dm}{require:Staff}{embed(title):t}"), "Hi")

    def test_a_variable_named_like_a_block_falls_back_when_the_block_can_not_run(self):
        self.assertEqual(text("{=(c):content}{c}"), "content")

    def test_embed_blocks_change_the_first_embed(self):
        template = {
            "content": "{embed(title):New}{embed(URL):https://example.com/a}{embed(description):D}{embed(color):#5989ab}{embed(timestamp):now}",
            "embeds": [{"title": "Old"}, {"title": "Second"}],
        }
        out = render_payload(template, Scope(now=at(2020, 1, 1)))
        self.assertEqual(out["content"], "")
        self.assertEqual(out["embeds"][0], {"title": "New", "url": "https://example.com/a", "description": "D", "color": 0x5989AB, "timestamp": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(out["embeds"][1], {"title": "Second"})

    def test_embed_blocks_start_an_embed_when_there_is_none(self):
        self.assertEqual(render_payload({"content": "{embed(title):New}"}, Scope())["embeds"], [{"title": "New"}])

    def test_bad_embed_values_change_nothing(self):
        out = render_payload({"content": "{embed(color):red}{embed(timestamp):later}", "embeds": [{"title": "t", "color": 5}]}, Scope())
        self.assertEqual(out["embeds"][0], {"title": "t", "color": 5})

    def test_an_unknown_embed_part_stays_as_written(self):
        self.assertEqual(text("{embed(nose):x}"), "{embed(nose):x}")

    def test_variables_reach_the_embed_from_the_text(self):
        out = render_payload({"content": "{=(desc):Hello {args}}", "embeds": [{"description": "{desc}!", "title": "{upper:{desc}}"}]}, Scope(args="you"))
        self.assertEqual(out["embeds"][0]["description"], "Hello you!")
        self.assertEqual(out["embeds"][0]["title"], "HELLO YOU")

    def test_links_can_be_templates(self):
        payload = {
            "content": "x", "buttons": [{"label": "{args}", "url": "https://example.com/{args}"}],
            "embeds": [{"title": "t", "url": "https://example.com/{args}", "thumbnail": {"url": "{user(avatar)}"}, "image": {"url": "{args}"},
                        "footer": {"text": "f", "icon_url": "{user(avatar)}"}, "author": {"name": "a", "url": "https://example.com/{args}", "icon_url": "{user(avatar)}"}}],
        }
        guild = FakeGuild()
        guild.me = FakeMember(guild, "bot")
        out = render_payload(payload, Scope(member=FakeMember(guild, "ada"), args="path"))
        embed = out["embeds"][0]
        self.assertEqual(embed["url"], "https://example.com/path")
        self.assertEqual(embed["thumbnail"]["url"], "https://cdn.example/avatar.png")
        self.assertEqual(embed["footer"]["icon_url"], "https://cdn.example/avatar.png")
        self.assertEqual(embed["author"]["url"], "https://example.com/path")
        self.assertNotIn("image", embed)  # "path" isn't a link, and Discord would refuse the whole message over it
        self.assertEqual(out["buttons"], [{"url": "https://example.com/path", "label": "path"}])

    def test_a_stored_message_can_hold_template_links_and_long_text(self):
        clean = clean_template_message({"content": "x" * 25000, "embeds": [{"title": "t", "url": "https://e.example/{args}", "thumbnail": {"url": "{user(avatar)}"}}]})
        self.assertEqual(len(clean["content"]), 25000)
        self.assertEqual(clean["embeds"][0]["thumbnail"]["url"], "{user(avatar)}")
        with self.assertRaises(Invalid):
            clean_template_message({"content": "x" * 25001})
        with self.assertRaises(Invalid):
            clean_template_message({"embeds": [{"title": "t", "url": "javascript:{alert}"}]})


class OutsideTextStaysDataTest(unittest.TestCase):
    """A member's words can't change what a template does, however they're written."""

    def test_a_comparison_operator_in_a_message_can_not_pass_a_check(self):
        self.assertEqual(text("{if({args}==secret):yes|no}", args="x==x"), "no")
        self.assertEqual(text("{any({args}==secret):yes|no}", args="x==x"), "no")
        self.assertEqual(text("{if({args}!=secret):yes|no}", args="secret!=x"), "yes")
        self.assertEqual(text("{if({args}==secret):yes|no}", args="secret"), "yes")

    def test_a_number_check_still_reads_the_number(self):
        self.assertEqual(text("{if({1}>5):big|small}", args="9"), "big")

    def test_a_parenthesis_or_colon_can_not_break_the_block_around_it(self):
        self.assertEqual(text("{if({args}==secret):yes|no}", args=")"), "no")
        self.assertEqual(text("{if({args}==a:b):yes|no}", args="a:b"), "yes")
        self.assertEqual(text("{upper:{args}}", args="a) b: c"), "A) B: C")

    def test_a_pipe_can_not_become_the_else(self):
        self.assertEqual(text("{if(1==1):You said {args}|Nothing}", args="a|b"), "You said a|b")

    def test_a_member_can_not_rewrite_which_command_runs(self):
        template = "{cmd:{if({args}==pass):role add 1 Verified|echo no}}"
        self.assertEqual(run(template, args="yes==yes):ban 1|x").actions.commands, [("echo no", False)])
        self.assertEqual(run(template, args="pass").actions.commands, [("role add 1 Verified", False)])

    def test_double_brackets_on_a_member_s_words_can_not_run_a_block(self):
        for typed in ("cmdAdmin:ban 1", "cmd:ban 1", "delete", "dm", "=(x):y", "redirect:logs"):
            interpreter = run("{{args}}", args=typed)
            self.assertEqual(interpreter.output, "{%s}" % typed)
            self.assertEqual(interpreter.actions.commands, [])
            self.assertFalse(interpreter.actions.delete)
            self.assertIsNone(interpreter.actions.destination)

    def test_a_block_name_from_a_member_with_the_rest_from_the_template_can_not_run(self):
        self.assertEqual(run("{{args}:ban 1}", args="cmdAdmin").actions.commands, [])
        self.assertEqual(run("{{lower:{args}}:ban 1}", args="CMD").actions.commands, [])
        self.assertEqual(run("{{urlencode:{args}}:ban 1}", args="cmd").actions.commands, [])
        self.assertEqual(run("{{replace(x,y):{args}}:ban 1}", args="cmd").actions.commands, [])

    def test_a_block_name_the_author_builds_still_runs(self):
        self.assertEqual(run("{=(what):cmd}{{what}:ping}").actions.commands, [("ping", False)])
        self.assertEqual(run("{{if(1==1):cmd|c}:ping}").actions.commands, [("ping", False)])

    def test_a_member_s_words_can_name_a_variable(self):
        self.assertEqual(text("{=(a):first}{=(b):second}{{args}}", args="b"), "second")
        self.assertEqual(text("{{args}}", args="2"), "2")

    def test_braces_in_a_message_are_text(self):
        self.assertEqual(text("{args}", args="{user}{cmd:ban 1}"), "{user}{cmd:ban 1}")
        self.assertEqual(run("{if(1==1):{args}}", args="{cmd:ban 1}").actions.commands, [])

    def test_a_nickname_is_data_too(self):
        guild = FakeGuild()
        guild.me = FakeMember(guild, "bot")
        sneaky = FakeMember(guild, "x")
        sneaky.nick = "a==a"
        self.assertEqual(render_text("{if({user}==b):yes|no}", Scope(member=sneaky)), "no")

    def test_values_from_a_feature_are_data_too(self):
        self.assertEqual(text("{if({reason}==b):yes|no}", extra={"reason": "a==a"}), "no")

    def test_private_characters_a_member_types_can_not_pass_for_shielded_text(self):
        hidden = "".join(chr(0x10FF00 + ord(c)) for c in "x==x")
        self.assertEqual(text("{if({args}==secret):yes|no}", args=hidden), "no")
        self.assertEqual(text("[{args}]", args=hidden + "ok"), "[ok]")
        self.assertEqual(text(hidden + "{upper:a}"), "A")

    def test_lists_a_member_writes_still_split(self):
        self.assertEqual(text("{list(1):{args}}", args="a,b"), "b")
        self.assertEqual(text("{args(2):,}", args="a,b"), "b")
        self.assertEqual(text("{join(-):{args}}", args="a b"), "a-b")
        self.assertEqual(text("{replace(a,z):{args}}", args="aba"), "zbz")
        self.assertEqual(text("{=(x):{args}}{x(1):|}", args="p|q"), "p")

    def test_a_comma_inside_a_filled_in_value_does_not_move_the_divider(self):
        self.assertEqual(text("{replace({args},Z):x,y!}", args="x,y"), "Z!")

    def test_text_blocks_keep_the_author_s_pipe_working_around_a_member_s_words(self):
        self.assertEqual(text("{if(1==1):{upper:{args}}|no}", args="a|b"), "A|B")
        self.assertEqual(text("{if(1==2):{lower:YES {args}|NO}}", args="x"), "no")

    def test_a_tag_s_output_reads_the_way_the_member_typed_it(self):
        typed = "(a|b)=={c}:~,%"
        self.assertEqual(text("{args}", args=typed), typed)
        self.assertEqual(text("{=(v):{args}}{if(1==1):{v}}", args=typed), typed)


class LimitsTest(unittest.TestCase):
    """Nothing here may take long, use much memory, or raise."""

    def setUp(self):
        self.started = time.monotonic()

    def tearDown(self):
        self.assertLess(time.monotonic() - self.started, 2.0, "a limit took too long to stop this")

    def assertStopped(self, template: str, **scope) -> None:
        with self.assertRaises(LimitReached):
            Interpreter(Scope(**scope)).render(template)
        self.assertEqual(text(template, **scope), template)

    def test_a_doubling_variable_stops(self):
        self.assertStopped("{=(x):lol}" + "{=(x):{x}{x}}" * 60 + "{x}")

    def test_doubling_through_words_stops(self):
        self.assertStopped("{=(x):abcdefghij}" + "{=(x):{x} {x(+9999)} {x(1+)}}" * 40 + "{x}")

    def test_a_growing_replace_stops_before_building_it(self):
        self.assertStopped("{=(x):%s}{replace(,%s):{x}}" % ("a" * 5000, "b" * 5000))
        self.assertStopped("{=(x):%s}{join(%s):{x}}" % ("a " * 5000, "b" * 5000))
        self.assertStopped("{replace(a,%s):%s}" % ("b" * 2000, "a" * 2000))

    def test_too_much_nesting_stops_without_overflowing_the_stack(self):
        self.assertStopped("{" * 12000 + "x" + "}" * 12000)

    def test_too_long_is_left_alone(self):
        long = "{a}" * 40000
        self.assertEqual(text(long), long)

    def test_too_many_blocks_is_left_alone(self):
        many = "{}" * (MAX_BLOCKS + 1)
        self.assertEqual(text(many), many)

    def test_a_full_size_template_of_small_blocks_finishes(self):
        template = "{=(a):1}" + "{a}" * 8000
        self.assertEqual(text(template), "1" * 8000)

    def test_repeated_splits_of_a_large_value_stop(self):
        self.assertStopped("{=(x):%s}" % ("word " * 19000) + "{x(1)}" * 900)

    def test_a_block_can_not_produce_more_than_the_limit(self):
        self.assertStopped("{=(x):%s}{=(y):{x}{x}{x}}{y}" % ("a" * 40000))
        self.assertGreater(MAX_VALUE, 25000)

    def test_many_small_outputs_can_not_add_up_past_the_limit(self):
        self.assertStopped("{=(x):%s}" % ("a" * 9000) + "{x}" * 12)

    def test_a_date_format_can_not_write_a_page(self):
        long = "{strf:%s}" % ("%c" * 1001)
        self.assertEqual(text(long), long)
        self.assertLess(len(text("{strf:%s}" % ("%99d" * 400))), 42000)

    def test_a_member_list_is_only_walked_once(self):
        calls = []

        class Counted:
            name, id, member_count, roles, channels, created_at, icon, owner, owner_id = "g", 1, 4, [], [], None, None, None, 1

            @property
            def members(self):
                calls.append(1)
                return [SimpleNamespace(bot=False, status="offline")]

        render_text("{server(random)}" * 200 + "{server(randomonline)}" * 200 + "{server(bots)}" * 200, Scope(guild=Counted()))
        self.assertEqual(len(calls), 1)

    def test_the_budget_covers_a_whole_message(self):
        big = "{=(x):%s}" % ("a" * 90000)
        interpreter = Interpreter(Scope())
        with self.assertRaises(LimitReached):
            for _ in range(200):
                interpreter.render(big)

    def test_render_payload_keeps_what_it_can_not_finish_as_written(self):
        template = "{=(x):a}" + "{=(x):{x}{x}}" * 60
        out = render_payload({"content": template, "embeds": [{"title": "{upper:t}"}]}, Scope())
        self.assertEqual(out["content"], template)
        self.assertEqual(out["embeds"][0]["title"], "{upper:t}")

    def test_render_strict_refuses_instead(self):
        with self.assertRaises(LimitReached):
            render_strict({"content": "{=(x):a}" + "{=(x):{x}{x}}" * 60}, Scope())


class NeverRaisesTest(unittest.TestCase):
    PIECES = [
        "{", "}", "(", ")", ":", "|", "~", ",", "==", "!=", ">", "<", ">=", "<=", "%", "^", "+", "-", "*", "/", " ", "\n", "x", "1", "0", "-1", "9" * 50, "é", "😀", "\x00", "\u202e", "\U0010ff41",
        "{if(", "{any(", "{all(", "{break(", "{stop(", "{=(", "{math:", "{m:", "{random:", "{#(", "{range:", "{rangef:", "{5050:", "{strf(", "{td(", "{list(", "{cycle(", "{index(", "{in(",
        "{contains(", "{ord:", "{lower:", "{upper:", "{join(", "{replace(", "{urlencode:", "{substr(", "{delete}", "{dm}", "{redirect:", "{require(", "{blacklist(", "{react:", "{reactu:",
        "{embed(title):", "{embed(color):", "{embed(timestamp):", "{cmd:", "{cmdAdmin:", "{args}", "{message}", "{1}", "{user}", "{user(id)}", "{server(random)}", "{channel(topic)}", "{target(color)}",
        "%A", "%-d", "%999d", "trunc(", "exp(", "1e308", "pi", "e",
    ]

    def test_random_nonsense_never_raises(self):
        generator = random.Random(20261105)
        guild = FakeGuild()
        guild.me = FakeMember(guild, "bot")
        member = FakeMember(guild, "ada")
        channel = FakeChannel(guild, "general")
        for _ in range(3000):
            template = "".join(generator.choice(self.PIECES) for _ in range(generator.randint(1, 30)))
            scope = Scope(guild=guild, member=member, channel=channel, args="".join(generator.choice(self.PIECES) for _ in range(5)))
            self.assertIsInstance(render_text(template, scope), str)
            out = render_payload({"content": template, "embeds": [{"title": template, "color": 1, "image": {"url": template}, "fields": [{"name": template, "value": template}]}]}, scope)
            self.assertIsInstance(out["content"], str)

    def test_odd_scopes_do_not_raise(self):
        for scope in (Scope(guild=object(), member=object(), channel=object(), target=object(), role=object()), Scope(args=None, uses=None, extra=None)):
            self.assertIsInstance(render_text("{user}{user(id)}{target(name)}{server}{server(random)}{channel(topic)}{role(name)}{args}{uses}", scope), str)

    def test_garbage_payloads_do_not_raise(self):
        for payload in (None, 5, "text", [], {"content": 5}, {"embeds": "x"}, {"embeds": [5, None, {"fields": "x"}, {"fields": [None, {"name": 5, "value": []}]}]}, {"buttons": [5, {"url": 5}]}, {"embeds": [{"footer": "x", "author": 5, "image": 5, "thumbnail": "x"}]}):
            out = render_payload(payload, Scope())
            self.assertEqual(set(out), {"content", "embeds", "buttons"})

    def test_empty_input_comes_back(self):
        self.assertEqual(render_text("", Scope()), "")
        self.assertIsNone(render_text(None, Scope()))
        self.assertEqual(render_text("no blocks ", Scope()), "no blocks ")


class FitTest(unittest.TestCase):
    def test_text_is_cut_to_discord_s_limits(self):
        out = fit_payload({
            "content": "x" * 3000,
            "embeds": [{"title": "t" * 300, "description": "d" * 5000, "footer": {"text": "f" * 3000}, "author": {"name": "a" * 300}, "fields": [{"name": "n" * 300, "value": "v" * 2000, "inline": True}]}],
        })
        self.assertEqual(len(out["content"]), 2000)
        embed = out["embeds"][0]
        self.assertEqual(len(embed["title"]), 256)
        self.assertEqual(len(embed["description"]), 4096)
        self.assertEqual(len(embed["footer"]["text"]), 1648)  # the 6000 characters a message's embeds share run out here
        self.assertNotIn("author", embed)
        self.assertNotIn("fields", embed)
        self.assertEqual(len(fit_payload({"embeds": [{"author": {"name": "a" * 300}, "fields": [{"name": "n" * 300, "value": "v" * 2000}]}]})["embeds"][0]["fields"][0]["value"]), 1024)

    def test_all_embeds_share_6000_characters(self):
        out = fit_payload({"embeds": [{"description": "a" * 4000}, {"description": "b" * 4000}, {"description": "c" * 4000}]})
        self.assertEqual(sum(len(e["description"]) for e in out["embeds"]), 6000)
        self.assertEqual(len(out["embeds"]), 2)

    def test_counts_are_cut(self):
        out = fit_payload({"embeds": [{"title": str(i)} for i in range(15)], "buttons": [{"label": str(i), "url": "https://e.example"} for i in range(30)]})
        self.assertEqual((len(out["embeds"]), len(out["buttons"])), (10, 25))
        out = fit_payload({"embeds": [{"fields": [{"name": "n", "value": "v"}] * 30}]})
        self.assertEqual(len(out["embeds"][0]["fields"]), 25)

    def test_what_discord_would_refuse_is_dropped(self):
        out = fit_payload({"embeds": [{
            "title": "t", "url": "not a link", "color": "red", "timestamp": "soon", "image": {"url": "nope"}, "thumbnail": {"url": "ftp://x"},
            "footer": {"text": "f", "icon_url": "nope"}, "author": {"name": "a", "url": "nope", "icon_url": "nope"},
            "fields": [{"name": "", "value": "v"}, {"name": "n", "value": "  "}, {"name": "kept", "value": "v"}],
        }], "buttons": [{"label": "x", "url": "nope"}, {"url": "https://e.example"}]})
        self.assertEqual(out["embeds"], [{"title": "t", "footer": {"text": "f"}, "author": {"name": "a"}, "fields": [{"name": "kept", "value": "v", "inline": False}]}])
        self.assertEqual(out["buttons"], [])

    def test_an_embed_left_empty_is_dropped(self):
        self.assertEqual(fit_payload({"embeds": [{"color": 5}, {}]})["embeds"], [])

    def test_good_values_pass_unchanged(self):
        embed = {
            "title": "t", "url": "https://e.example", "description": "d", "color": 5, "timestamp": "2020-01-01T00:00:00+00:00", "footer": {"text": "f", "icon_url": "https://e.example/i.png"},
            "author": {"name": "a", "url": "https://e.example", "icon_url": "https://e.example/i.png"}, "image": {"url": "https://e.example/i.png"}, "thumbnail": {"url": "attachment://t.png"},
            "fields": [{"name": "n", "value": "v", "inline": True}],
        }
        self.assertEqual(fit_payload({"content": "c", "embeds": [embed], "buttons": [{"url": "https://e.example", "label": "l", "emoji": "x"}]}), {
            "content": "c", "embeds": [embed], "buttons": [{"url": "https://e.example", "label": "l", "emoji": "x"}],
        })


if __name__ == "__main__":
    unittest.main()


class EmbedBlocksTest(unittest.TestCase):
    """A tag made by command has no embed builder behind it, so its embed blocks start one."""

    def payload(self, content, embeds=None):
        return render_payload({"content": content, "embeds": embeds or [], "buttons": []}, Scope())

    def test_a_block_starts_an_embed_when_there_is_none(self):
        out = self.payload("{embed(title):T}body")
        self.assertEqual(out["content"], "body")
        self.assertEqual(out["embeds"][0]["title"], "T")

    def test_a_tag_that_is_only_an_embed_block_has_something_to_post(self):
        out = self.payload("{embed(description):Hello}")
        self.assertTrue(messages.has_content(out))

    def test_a_color_alone_makes_no_embed(self):
        self.assertEqual(self.payload("{embed(color):#5989ab}text")["embeds"], [])

    def test_an_existing_embed_is_still_the_one_changed(self):
        out = self.payload("{embed(title):T}", [{"description": "D"}, {"description": "E"}])
        self.assertEqual([embed.get("title") for embed in out["embeds"]], ["T", None])

    def test_no_blocks_means_no_embed(self):
        self.assertEqual(self.payload("plain")["embeds"], [])


class EmbedOrderTest(unittest.TestCase):
    def test_parts_run_in_the_order_the_page_shows_them(self):
        embed = {
            "footer": {"text": "{a}{b}{c}"}, "fields": [{"name": "n", "value": "{=(c):3}{a}{b}"}], "description": "{=(b):2}{a}", "title": "{=(a):1}{zz}", "author": {"name": "{=(zz):0}x"},
        }
        out = render_payload({"content": "", "embeds": [embed], "buttons": []}, Scope())["embeds"][0]
        self.assertEqual(out["title"], "0", "the author comes before the title")
        self.assertEqual(out["description"], "1", "the title comes before the description")
        self.assertEqual(out["fields"][0]["value"], "12", "the description comes before the fields")
        self.assertEqual(out["footer"]["text"], "123", "the footer comes last")

    def test_stop_ends_only_the_text_it_is_in(self):
        rendered, actions, _ = render_strict({"content": "{stop(1==1):end}after", "embeds": [{"title": "{cmd:ping}T"}], "buttons": []}, Scope())
        self.assertEqual(rendered["content"], "end")
        self.assertEqual(actions.commands, [("ping", False)], "the reference says no later block in that text runs")


class MathDepthTest(unittest.TestCase):
    def test_the_limit_counts_each_level_once(self):
        self.assertEqual(evaluate("(" * MAX_DEPTH + "1" + ")" * MAX_DEPTH), "1.0")
        self.assertIsNone(evaluate("(" * (MAX_DEPTH + 1) + "1" + ")" * (MAX_DEPTH + 1)))

    def test_a_function_counts_once_too(self):
        self.assertEqual(evaluate("abs(" * MAX_DEPTH + "1" + ")" * MAX_DEPTH), "1.0")
        self.assertIsNone(evaluate("abs(" * (MAX_DEPTH + 1) + "1" + ")" * (MAX_DEPTH + 1)))


class NumberFormatsTest(unittest.TestCase):
    EDGES = (0, 1, 2, 3, 4, 10, 11, 12, 13, 14, 20, 21, 22, 23, 100, 101, 111, 112, 113, 121, 999, 1000, 1011, 8102, 123456, 1000000, 1234567, 10 ** 12, -1, -2, -11, -21, -8102)

    def test_ord_with_no_format_is_what_it_always_gave(self):
        for number in self.EDGES:
            self.assertEqual(text("{ord:%d}" % number), ordinal(number), number)
            self.assertEqual(numerals.ordinal(number, "commas"), ordinal(number), number)
        self.assertEqual(text("{ord:8102}"), "8,102nd")
        self.assertEqual(text("{ord:8102.0}"), "8,102nd")

    def test_a_format_nobody_has_heard_of_changes_nothing(self):
        self.assertEqual(text("{ord(roman):8102}"), "8,102nd")
        self.assertEqual(text("{ord():8102}"), "8,102nd")
        self.assertEqual(text("{ord(plain):word}"), "word")

    def test_ord_in_each_format(self):
        for style, expected in [("plain", "1234567th"), ("commas", "1,234,567th"), ("dots", "1.234.567th"), ("indian", "12,34,567th"), ("arabic", arabic("1,234,567") + "th"), ("PLAIN", "1234567th"), (" dots ", "1.234.567th")]:
            self.assertEqual(text("{ord(%s):1234567}" % style), expected, style)

    def test_the_ending_follows_the_number_in_every_format(self):
        for number, ending in [(0, "th"), (1, "st"), (2, "nd"), (3, "rd"), (4, "th"), (11, "th"), (12, "th"), (13, "th"), (21, "st"), (101, "st"), (111, "th"), (112, "th"), (113, "th"), (1000000, "th"), (1000001, "st"), (-1, "st"), (-12, "th")]:
            for style in numerals.FORMATS:
                self.assertTrue(text("{ord(%s):%d}" % (style, number)).endswith(ending), (number, style))
            self.assertEqual(text("{ord(plain):%d}" % number), f"{number}{ending}")

    def test_num_groups_with_no_ending(self):
        for style, expected in [(None, "1,234,567"), ("plain", "1234567"), ("commas", "1,234,567"), ("dots", "1.234.567"), ("indian", "12,34,567"), ("arabic", arabic("1,234,567"))]:
            self.assertEqual(text("{num(%s):1234567}" % style if style else "{num:1234567}"), expected, style)

    def test_small_and_negative_numbers(self):
        for style in ("plain", "commas", "dots", "indian"):
            self.assertEqual([numerals.grouped(number, style) for number in (0, 7, 999)], ["0", "7", "999"], style)
        self.assertEqual([numerals.grouped(number, "arabic") for number in (0, 7, 999, 1000)], [arabic("0"), arabic("7"), arabic("999"), arabic("1,000")])
        self.assertEqual([numerals.grouped(-8102, style) for style in ("plain", "commas", "dots", "indian", "arabic")], ["-8102", "-8,102", "-8.102", "-8,102", "-" + arabic("8,102")])
        self.assertEqual([numerals.grouped(number, "indian") for number in (999, 1000, 99999, 100000, 9999999, 10000000)], ["999", "1,000", "99,999", "1,00,000", "99,99,999", "1,00,00,000"])

    def test_num_leaves_what_is_not_a_whole_number_as_written(self):
        for template in ("{num:word}", "{num:12.5}", "{num:}", "{num(roman):5}", "{num:" + "9" * 41 + "}", "{num}"):
            self.assertEqual(text(template), template, template)
        self.assertEqual(text("{num:{m:1000+234}}"), "1,234")

    def test_a_variable_called_num_or_title_still_reads(self):
        self.assertEqual(text("{=(num):a,b,c}{num} {num(2):,}"), "a,b,c b")
        self.assertEqual(text("{=(title):one two}{title} {title(2)}"), "one two two")
        self.assertEqual(text("{=(title):a,b,c}{title(2):,}"), "b", "a position and a divider read the variable, as they did before the block existed")
        self.assertEqual(text("{=(since):then}{since} {since(1)}"), "then then")

    def test_a_number_from_outside_stays_data(self):
        self.assertEqual(text("{if({num:{count}}==1,234):yes|no}", extra={"count": "1234"}), "yes")
        self.assertEqual(text("{if({num:{count}}==1,234):yes|no}", extra={"count": "1==1"}), "no", "what isn't a number stays as the member's text, which can't pass for a comparison")


class OwnVariablesWinTest(unittest.TestCase):
    """title, num and since are blocks newer than templates that may hold a variable by one of those names. That variable wins in every
    form, so a stored template reads as it did before the blocks existed, which is how a variable by any other name reads."""

    NOW = at(2026, 1, 2, 3, 4, 5)
    FORMS = ("{%s}", "{%s:x}", "{%s:}", "{%s:5}", "{%s:1234567}", "{%s:2024-01-01}", "{%s:2024-01-01 10:00:00}", "{%s(2)}", "{%s(2):,}", "{%s(+2)}",
             "{%s(commas):1234}", "{%s(dots):5}", "{%s(2024-01-01):2024-01-03}")

    def test_a_template_s_own_variable_wins_in_every_form(self):
        for name in ("title", "num", "since", "Title", "NUM"):
            for value in ("My Title", "a,b,c", "7", "2025-01-01"):
                for form in self.FORMS:
                    own = "{=(%s):%s}" % (name, value) + form % name
                    other = "{=(other):%s}" % value + form % "other"
                    self.assertEqual(text(own, now=self.NOW), text(other, now=self.NOW), own)
        self.assertEqual(text("{=(title):My Title}{title:x}"), "My Title", "the review's case, as it printed before the block existed")

    def test_a_variable_from_outside_wins_the_same_way(self):
        for name in ("title", "num", "since"):
            for form in self.FORMS:
                self.assertEqual(text(form % name, now=self.NOW, extra={name: "Alert: 5 new"}), text(form % "other", now=self.NOW, extra={"other": "Alert: 5 new"}), (name, form))

    def test_a_block_s_name_is_put_in_lower_case_once(self):
        # A name is all of a block's text when it holds no colon or parenthesis, so for braces nested deep it grows with the depth, and every
        # extra pass over it costs a server time it pays for.
        calls = []

        class Counting(str):
            def lower(self):
                calls.append(str(self))
                return str.lower(self)

        with mock.patch.object(engine_module, "plain", lambda value: Counting(text_module.plain(value))):
            self.assertEqual(text("{upper:x}{title:y}{=(num):5}{num:7}{nope}"), "XY5{nope}")
        self.assertEqual(calls, ["upper", "title", "=", "num", "nope"], "once for each block")

    def test_with_no_variable_of_that_name_the_blocks_still_work(self):
        self.assertEqual(text("{title:tall ship} {num:1234567} {since:2025-01-02 03:04:05}", now=self.NOW), "Tall Ship 1,234,567 1 year")
        self.assertEqual(text("{=(other):x}{title:tall ship} {num(dots):1234}", extra={"titles": "y"}), "Tall Ship 1.234")


class FormatTextTest(unittest.TestCase):
    """A strf format the template wrote, read with a date from outside: the result stays data as a whole, as it always did, and the format's
    own text still counts as the template's, so a mention typed in it can ping where a greeting allows."""

    def test_what_it_gives_and_how_it_splits_stay_as_they_were(self):
        outside = {"when": "2020-03-01 10:00:00"}
        self.assertEqual(text("{strf({when}):Joined %Y. Ask <@123456789012345678>.}", extra=outside), "Joined 2020. Ask <@123456789012345678>.")
        self.assertEqual(text("{if(1==1):{strf({when}):%Y|b}|no}", extra=outside), "2020|b", "its pipe is data, so the if divides only at its own")
        self.assertEqual(text("{if({strf({when}):%Y}==2020):yes|no}", extra=outside), "yes")
        self.assertEqual(text("{title:{strf({when}):joined in %B, it's %Y}}", extra=outside), "Joined In March, It's 2020")
        self.assertEqual(text("{upper:{strf({when}):joined %b}} {lower:{strf({when}):JOINED %b}}", extra=outside), "JOINED MAR joined mar")
        self.assertEqual(text("{=(v):{strf({when}):a b %Y}}{v(2)} {v(3)} {replace(b,c):{v}}", extra=outside), "b 2020 a c 2020")
        self.assertEqual(text("{list(1):{strf({when}):x~y~%Y}} {strf({when}):%Y{when}}", extra=outside), "y 20202020-03-01 10:00:00")

    def test_the_format_s_own_text_counts_as_the_template_s_and_the_date_s_doesn_t(self):
        marked = Interpreter(Scope(extra={"when": "2020-03-01 10:00:00"})).render_marked("{strf({when}):Ask <@123456789012345678> in %Y.}")
        shown = plain(marked)
        self.assertEqual(shown, "Ask <@123456789012345678> in 2020.")
        mention = shown.index("<@")
        self.assertTrue(written_by_template(marked, mention, mention + len("<@123456789012345678>")))
        year = shown.index("2020")
        self.assertFalse(written_by_template(marked, year, year + 4), "the year came from outside")
        upper = Interpreter(Scope(extra={"when": "2020-03-01"})).render_marked("{upper:{strf({when}):ask <@123456789012345678>}}")
        self.assertTrue(written_by_template(upper, 4, 25), "and it stays the template's through a block that changes its letters")
        beside = Interpreter(Scope(extra={"when": "2020-03-01", "x": "<@123456789012345678>"})).render_marked("{upper:{strf({when}):ask }{x}}")
        self.assertEqual(plain(beside), "ASK <@123456789012345678>")
        self.assertFalse(written_by_template(beside, 4, 25), "while text from outside right beside it stays outside")
        format_from_outside = Interpreter(Scope(extra={"when": "2020-03-01", "f": "<@123456789012345678>"})).render_marked("{strf({when}):{f}}")
        self.assertFalse(written_by_template(format_from_outside, 0, 21), "a format from outside stays outside")

    def test_a_strf_inside_another_s_format_keeps_its_own_text_the_template_s(self):
        outside = {"when": "2020-03-01 10:00:00", "then": "2021-04-02 11:00:00", "f": "%Y <@123456789012345678>"}
        marked = Interpreter(Scope(extra=outside)).render_marked("{strf({when}):{strf({then}):Ask <@123456789012345678> on %d, 100%%} %Y}")
        shown = plain(marked)
        self.assertEqual(shown, "Ask <@123456789012345678> on 02, 100% 2020")
        mention = shown.index("<@")
        self.assertTrue(written_by_template(marked, mention, mention + len("<@123456789012345678>")), "the inner format's own text")
        for made in ("02", "2020"):
            at = shown.index(made)
            self.assertFalse(written_by_template(marked, at, at + len(made)), made)
        # The text reads as it always did: a code is found in the format's plain text, whatever spelled it.
        self.assertEqual(text("{strf({when}):{strf({then}):%%}Y} {strf({when}):{f}}", extra=outside), "2020 2020 <@123456789012345678>")
        from_outside = Interpreter(Scope(extra=outside)).render_marked("{strf({when}):{f}}")
        self.assertEqual(plain(from_outside), "2020 <@123456789012345678>")
        self.assertFalse(written_by_template(from_outside, 5, 26), "text from outside in a format stays outside")

    def test_the_marked_result_takes_one_pass_over_the_format(self):
        # One pass over the pattern, either way, makes each distinct code's text once. With a table and a literal text, the pattern's own text
        # comes from the literal and the codes' text goes through the table, as strf marks them with a date from outside: the whole still reads
        # as format_time's.
        moment = datetime.datetime(2020, 3, 1, 10, 4, 5, tzinfo=UTC)
        pattern = "%a, %-d %B %Y at %H:%M, %%, %Q, %^a, %3d" * 3  # no shorthand such as %c, whose codes are read through it again
        field = clock_module._FIELD
        found = [match.group(0) for match in field.finditer(pattern)]
        made = "".join(format_time(moment, code) for code in found)
        own = field.sub("", pattern).translate(text_module.KEEPING)
        calls, scans = [], []
        real = clock_module._field_text

        def counted(at, match):
            calls.append(match.group(0))
            return real(at, match)

        class Scanned:
            def __getattr__(self, name):
                scans.append(name)
                return getattr(field, name)

        for tables in ({}, {"codes": text_module.SHIELDING, "literal": pattern.translate(text_module.KEEPING)}):
            calls.clear()
            scans.clear()
            with mock.patch.object(clock_module, "_field_text", counted), mock.patch.object(clock_module, "_FIELD", Scanned()):
                marked = clock_module.format_time(moment, pattern, **tables)
            self.assertEqual(len(scans), 1, "one pass over the pattern")
            self.assertEqual(sorted(calls), sorted(set(found)), "each distinct code made once")
            self.assertEqual(plain(marked), format_time(moment, pattern))
        self.assertEqual(len(found), 30)
        kept = {chr(code) for code in text_module.KEEPING.values()}
        self.assertEqual("".join(ch for ch in marked if ch in kept), own, "the pattern's own text through its table")
        self.assertEqual("".join(ch for ch in marked if ch not in kept), text_module.shield(made), "the codes' text through theirs")

    def test_nobody_can_type_the_template_s_marks(self):
        self.assertEqual(text("[{x}]", extra={"x": "a" + chr(0x10FE41) + chr(0x10FF41) + "b"}), "[ab]")
        self.assertEqual(text("[" + chr(0x10FE41) + "]{=(a):b}"), "[]")


class TitleTest(unittest.TestCase):
    def test_each_word_starts_with_a_capital_and_the_rest_stays(self):
        for written, expected in [("tall ship", "Tall Ship"), ("TALL ship", "TALL Ship"), ("mcDonald o'neil", "McDonald O'neil"), ("it's here", "It's Here"), ("john.doe", "John.Doe"),
                                  ("3rd time", "3rd Time"), ("snake_case name", "Snake_case Name"), ("école normale", "École Normale"), ("straße ßen", "Straße ßen"), ("", "")]:
            self.assertEqual(text("{title:%s}" % written), expected, written)

    def test_a_word_can_start_in_the_template_and_end_in_a_name(self):
        self.assertEqual(text("{title:mc{name} and {name}}", extra={"name": "donald"}), "Mcdonald And Donald")

    def test_a_name_s_punctuation_stays_data(self):
        self.assertEqual(text("{if(1==1):{title:{name}}|else}", extra={"name": "a|b"}), "A|B", "a pipe in a name mustn't become the divider of the if around it")
        self.assertEqual(text("{if(1==2):then|{title:{name}}}", extra={"name": "x)|y:z"}), "X)|Y:Z")


class SinceTest(unittest.TestCase):
    NOW = at(2026, 1, 2, 3, 4, 5)

    def test_one_unit_the_largest(self):
        for date, expected in [("2020-02-03 04:05:06", "5 years"), ("2025-01-02 03:04:05", "1 year"), ("2025-10-01 00:00:00", "3 months"), ("2025-12-20 00:00:00", "1 week"),
                               ("2025-12-30 00:00:00", "3 days"), ("2026-01-02 01:00:00", "2 hours"), ("2026-01-02 03:03:05", "1 minute"), ("2026-01-02 03:04:01", "4 seconds"),
                               ("2026-01-02 03:04:05", "now"), ("2027-06-01 00:00:00", "1 year")]:
            self.assertEqual(text("{since:%s}" % date, now=self.NOW), expected, date)

    def test_between_two_dates_and_from_a_unix_time(self):
        self.assertEqual(text("{since(2026-01-01 00:00:00):2026-01-03 12:00:00}"), "2 days")
        self.assertEqual(text("{since:%d}" % at(2025, 1, 1), now=self.NOW), "1 year")

    def test_td_still_names_three(self):
        self.assertEqual(text("{td:2020-02-03 04:05:06}", now=self.NOW), "5 years, 10 months and 4 weeks")
        self.assertEqual(describe_gap(datetime.datetime(2022, 1, 1, tzinfo=UTC), datetime.datetime(2025, 3, 1, tzinfo=UTC), 1), "3 years")

    def test_what_is_not_a_date_stays_as_written(self):
        for template in ("{since:someday}", "{since(someday):2026-01-01 00:00:00}", "{since:}", "{since}"):
            self.assertEqual(text(template), template)


class GreetingObjectsTest(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild("Harbor")
        self.member = FakeMember(self.guild, "ada", joined=datetime.datetime(2025, 6, 7, 8, 9, 10, tzinfo=UTC), created=datetime.datetime(2020, 2, 3, 4, 5, 6, tzinfo=UTC))
        self.invite = Invite(code="harbor", uses=12, max_uses=50, max_age=3600, temporary=True, channel_id=444, created_at=datetime.datetime(2025, 12, 1, tzinfo=UTC),
                             expires_at=datetime.datetime(2026, 2, 1, tzinfo=UTC), inviter_id=555, inviter_name="Grace", inviter_username="grace", inviter_global_name="Grace")

    def read(self, template: str, **changes) -> str:
        return render_text(template, Scope(guild=self.guild, member=self.member, **changes))

    def test_what_a_person_adds(self):
        self.member.global_name = "Ada Lovelace"
        self.member.discriminator = "1234"
        for prop, expected in [("username", "ada"), ("global_name", "Ada Lovelace"), ("profile_name", "Ada Lovelace"), ("profile_avatar", "https://cdn.example/avatar.png"),
                               ("discriminator", "1234"), ("bot", "false"), ("pending", "false"), ("created_ago", "<t:1580702706:R>"), ("joined_ago", "<t:1749283750:R>")]:
            self.assertEqual(self.read("{user(%s)}" % prop), expected, prop)

    def test_a_profile_name_is_never_the_server_nickname(self):
        self.member.nick, self.member.global_name = "Countess", "Ada Lovelace"
        self.assertEqual(self.read("{user}|{user(nick)}|{user(profile_name)}|{user(global_name)}|{user(name)}"), "Countess|Countess|Ada Lovelace|Ada Lovelace|ada")

    def test_a_profile_name_without_a_global_name_is_the_username(self):
        self.member.nick, self.member.global_name = None, None
        self.assertEqual(self.read("{user(profile_name)}|[{user(global_name)}]"), "ada|[]")
        self.member.nick = "Countess"
        self.assertEqual(self.read("{user}|{user(profile_name)}"), "Countess|ada")

    def test_a_profile_name_keeps_the_old_tag_of_an_account_that_has_one(self):
        self.member.global_name, self.member.discriminator = None, "1234"
        self.assertEqual(self.read("{user(profile_name)}"), "ada#1234")
        for tag in ("0", "", None):
            self.member.discriminator = tag
            self.assertEqual(self.read("{user(profile_name)}"), "ada", tag)
        self.member.global_name, self.member.discriminator = "Ada Lovelace", "1234"
        self.assertEqual(self.read("{user(profile_name)}"), "Ada Lovelace")

    def test_a_profile_avatar_is_never_the_server_avatar(self):
        self.member.display_avatar = SimpleNamespace(url="https://cdn.example/in-this-server.png")
        self.assertEqual(self.read("{user(avatar)} {user(profile_avatar)}"), "https://cdn.example/in-this-server.png https://cdn.example/avatar.png")
        self.member.avatar, self.member.default_avatar = None, SimpleNamespace(url="https://cdn.example/default.png")
        self.assertEqual(self.read("{user(profile_avatar)}"), "https://cdn.example/default.png", "an account with no picture has Discord's default one")

    def test_a_name_someone_chose_cant_make_a_link(self):
        self.member.global_name = "[free nitro](https://a.test)"
        self.assertEqual(self.read("{user(global_name)} {user(profile_name)}"), "[free nitro]\\(https://a.test) [free nitro]\\(https://a.test)")

    def test_a_bot_and_someone_on_the_rules_screen(self):
        robot = FakeMember(self.guild, "robot", bot=True, pending=True)
        self.assertEqual(render_text("{user(bot)} {user(pending)}", Scope(member=robot)), "true true")
        self.assertEqual(self.read("{if({user(bot)}==true):bot|person}"), "person")
        self.assertEqual(render_text("{if({user(bot)}==true):bot|person}", Scope(member=robot)), "bot")

    def test_someone_with_less_to_read(self):
        bare = SimpleNamespace(id=1, name="gone", display_name="gone", mention="<@1>", joined_at=None, created_at=None)
        self.assertEqual(text("[{user(global_name)}][{user(discriminator)}][{user(bot)}][{user(pending)}][{user(joined_ago)}][{user(created_ago)}]", member=bare), "[][0][false][false][][]")
        self.assertEqual(text("[{user(profile_name)}][{user(profile_avatar)}]", member=bare), "[gone][]")

    def test_a_server_s_splash_and_banner(self):
        self.assertEqual(self.read("[{server(splash)}][{server(banner)}]"), "[][]")
        self.guild.splash = SimpleNamespace(url="https://cdn.example/splash.png")
        self.guild.banner = SimpleNamespace(url="https://cdn.example/banner.png")
        self.assertEqual(self.read("{server(splash)} {server(banner)}"), "https://cdn.example/splash.png https://cdn.example/banner.png")

    def test_the_invite_and_its_properties(self):
        extra = {"invite": objects.invite(self.invite)}
        for prop, expected in [("code", "harbor"), ("uses", "12"), ("max_uses", "50"), ("max_age", "3600"), ("temporary", "true"), ("channel", "444"), ("created_at", "2025-12-01 00:00:00"),
                               ("expires_at", "2026-02-01 00:00:00"), ("created_ago", "<t:1764547200:R>"), ("expires_in", "<t:1769904000:R>"), ("inviter", "Grace"),
                               ("inviter_username", "grace"), ("inviter_global_name", "Grace"), ("inviter_mention", "<@555>"), ("inviter_id", "555")]:
            self.assertEqual(self.read("{invite(%s)}" % prop, extra=extra), expected, prop)
        self.assertEqual(self.read("{invite}", extra=extra), "harbor")
        self.assertEqual(self.read("{invite(shoe)}", extra=extra), "{invite(shoe)}")
        self.assertEqual(set(objects.invite(None).properties), {"code", "uses", "max_uses", "max_age", "temporary", "channel", "created_at", "expires_at", "created_ago", "expires_in",
                                                                "inviter", "inviter_username", "inviter_global_name", "inviter_mention", "inviter_id"})

    def test_an_invite_nobody_could_tell(self):
        extra = {"invite": objects.invite(None)}
        self.assertEqual(self.read("{invite}|{invite(code)}|{invite(inviter)}|{invite(inviter_mention)}", extra=extra), "Unknown|Unknown|Unknown|Unknown")
        self.assertEqual(self.read("{invite(inviter_username)}|{invite(inviter_global_name)}", extra=extra), "Unknown|Unknown")
        self.assertEqual(self.read("{invite(uses)}|{invite(max_uses)}|{invite(max_age)}|{invite(channel)}|{invite(inviter_id)}|{invite(temporary)}", extra=extra), "0|0|0|0|0|false")
        self.assertEqual(self.read("[{invite(created_at)}][{invite(expires_at)}][{invite(created_ago)}][{invite(expires_in)}]", extra=extra), "[][][][]")
        self.assertEqual(self.read("{if({invite(inviter_id)}!=0):known|unknown}", extra=extra), "unknown")

    def test_a_vanity_link_has_no_inviter_and_no_end(self):
        extra = {"invite": objects.invite(Invite(code="harbor", uses=900))}
        self.assertEqual(self.read("{invite}|{invite(uses)}|[{invite(inviter)}]|[{invite(inviter_mention)}]|{invite(inviter_id)}|[{invite(expires_at)}]|{invite(max_uses)}", extra=extra), "harbor|900|[]|[]|0|[]|0")
        self.assertEqual(self.read("[{invite(inviter_username)}][{invite(inviter_global_name)}]", extra=extra), "[][]")

    def test_an_inviter_s_name_is_data(self):
        hostile = "[free nitro](https://a.test) {delete} a|b"
        for field, prop in [("inviter_name", "inviter"), ("inviter_username", "inviter_username"), ("inviter_global_name", "inviter_global_name")]:
            out = self.read("{if(1==1):{invite(%s)}|else}" % prop, extra={"invite": objects.invite(Invite(code="x", inviter_id=5, **{field: hostile}))})
            self.assertEqual(out, "[free nitro]\\(https://a.test) {delete} a|b", prop)

    def test_without_a_greeting_the_names_stay_as_written(self):
        self.assertEqual(self.read("{joined} {invite} {invite(uses)}"), "{joined} {invite} {invite(uses)}")

    def test_the_joined_count_is_a_number_other_blocks_take(self):
        self.assertEqual(self.read("{joined} {ord:{joined}} {ord(plain):{joined}} {num(dots):{joined}}", extra={"joined": "64097"}), "64097 64,097th 64097th 64.097")

