"""The template language: what each block gives, that members' text stays data, and that no template can hang or crash anything."""
import datetime
import random
import time
import unittest
from types import SimpleNamespace

from captainguild.core import messages
from captainguild.core.tagscript import (
    LimitReached, MAX_BLOCKS, MAX_COMMANDS, MAX_REACTIONS, MAX_VALUE, Interpreter, Scope, clean_template_message, fit_payload, render_payload, render_strict, render_text, split_block,
)
from captainguild.core.tagscript.clock import describe_gap, format_time, parse_when
from captainguild.core.tagscript.mathexpr import MAX_DEPTH, evaluate
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

