"""docs/template-language.md, run against the engine, so the reference can't drift from the code."""
import datetime
import re
import unittest
from pathlib import Path

from captainguild.core.tagscript import MAX_BLOCKS, MAX_COMMANDS, MAX_REACTIONS, MAX_SECONDS, MAX_VALUE, MAX_WORK, TEMPLATE_MAX, Interpreter, Scope, render_text
from captainguild.core.invitewatch import Invite
from captainguild.core.tagscript import objects
from captainguild.core.tagscript.blocks import BLOCKS
from captainguild.core.tagscript.mathexpr import MAX_DEPTH, MAX_INT_BITS, MAX_LENGTH
from captainguild.core.tagscript.numerals import FORMATS
from captainguild.core.tagscript.objects import _CHANNEL, _PERSON
from tests.fakes import FakeChannel, FakeGuild, FakeMember

DOC = Path(__file__).resolve().parent.parent / "docs" / "template-language.md"
UTC = datetime.timezone.utc
_CELL = re.compile(r"(?<!\\)\|")
_SPAN = re.compile(r"`([^`]*)`")


def scope() -> Scope:
    """The values the reference says its examples run against."""
    guild = FakeGuild("Harbor")
    guild.id = 333333333333333333
    guild.created_at = datetime.datetime(2019, 1, 1, tzinfo=UTC)
    guild.default_role.id = guild.id
    crew = guild.role("Crew", 2, color=0x5989AB)
    crew.id = 222222222222222222
    crew.mention = f"<@&{crew.id}>"
    guild.me = FakeMember(guild, "bot", bot=True)
    guild.owner_id = FakeMember(guild, "owner").id
    joined = datetime.datetime(2025, 6, 7, 8, 9, 10, tzinfo=UTC)
    ada = FakeMember(guild, "ada", crew, joined=joined, created=datetime.datetime(2020, 2, 3, 4, 5, 6, tzinfo=UTC))
    ada.id = 111111111111111111
    ada.mention = f"<@{ada.id}>"
    ada.nick = "Ada"
    ada.global_name = "Ada Lovelace"
    grace = FakeMember(guild, "grace")
    grace.id = 555555555555555555
    grace.mention = f"<@{grace.id}>"
    grace.nick = "Grace"
    channel = FakeChannel(guild, "general")
    channel.id = 444444444444444444
    channel.mention = f"<#{channel.id}>"
    channel.topic = "Say hello"
    channel.position = 3
    channel.slowmode_delay = 30
    for name in ("one", "two", "three"):
        FakeChannel(guild, name)
    invite = Invite(
        code="harbor", uses=12, max_uses=50, max_age=5356800, channel_id=channel.id, created_at=datetime.datetime(2025, 12, 1, tzinfo=UTC),
        expires_at=datetime.datetime(2026, 2, 1, tzinfo=UTC), inviter_id=grace.id, inviter_name="Grace", inviter_username="grace", inviter_global_name="Grace",
    )
    return Scope(
        guild=guild, member=ada, channel=channel, target=grace, args="one two three", uses=42, role=crew, role_added=True,
        now=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp(), extra={"joined": "8102", "invite": objects.invite(invite)},
    )


def tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Every table in the file as (header, rows), split the way the docs page splits them."""
    out, rows = [], []
    for line in [*text.splitlines(), ""]:
        if line.startswith("|"):
            rows.append([cell.strip().replace("\\|", "|") for cell in _CELL.split(line.strip().strip("|"))])
        elif rows:
            out.append((rows[0], [row for row in rows[1:] if not all(re.fullmatch(r":?-+:?", cell) for cell in row)]))
            rows = []
    return out


def examples(text: str) -> list[tuple[str, str]]:
    found = []
    for header, rows in tables(text):
        if "Example" in header and "Result" in header:
            for row in rows:
                found.append((row[header.index("Example")], row[header.index("Result")]))
    return found


def allowed_results(cell: str) -> set[str]:
    """A result cell is code spans and the word nothing, with "or" between them."""
    remainder = _SPAN.sub("", cell).replace(" or ", " ").strip()
    outcomes = set(_SPAN.findall(cell))
    if "nothing" in remainder:
        outcomes.add("")
        remainder = remainder.replace("nothing", "").strip()
    assert not remainder, f"a result cell can only hold code spans and the word nothing: {cell}"
    return outcomes


class ReferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding="utf-8")
        cls.examples = examples(cls.text)

    def setUp(self):
        self.scope = scope()

    def test_there_are_examples(self):
        self.assertGreater(len(self.examples), 150)

    def test_every_example_gives_its_result(self):
        for example, result in self.examples:
            spans = _SPAN.findall(example)
            self.assertEqual(len(spans), 1, f"an example cell holds one code span: {example}")
            wanted = allowed_results(result)
            # A result with a choice in it can take a few tries to show that every outcome is real, and one try is enough to catch a wrong one.
            for _ in range(40 if len(wanted) > 1 else 1):
                with self.subTest(example=spans[0]):
                    self.assertIn(render_text(spans[0], self.scope), wanted)

    def test_every_listed_outcome_of_a_choice_can_happen(self):
        for example, result in self.examples:
            wanted = allowed_results(result)
            if len(wanted) > 1:
                seen = {render_text(_SPAN.findall(example)[0], self.scope) for _ in range(300)}
                self.assertEqual(seen, wanted, example)

    def test_worked_examples(self):
        fences = re.findall(r"```(\w+)\n(.*?)```", self.text, re.DOTALL)
        results = [(fences[i][1], fence[1]) for i, fence in enumerate(fences[1:]) if fence[0] == "result"]
        self.assertEqual(len(results), 1)
        for template, result in results:
            self.assertEqual(render_text(template, self.scope), result.strip(), template)

    def test_the_first_worked_example_does_what_the_text_says(self):
        template = re.findall(r"```template\n(.*?)```", self.text, re.DOTALL)[0]
        run = Interpreter(self.scope)
        self.assertEqual(run.render(template), "")
        self.assertEqual(run.actions.commands, [(f"role add {self.scope.target.id} Crew", False)])
        self.assertTrue(run.actions.silent)
        keys = [key for key, _ in run.actions.embed]
        self.assertEqual(keys, ["description", "color"])
        self.assertIn(str(self.scope.target.id), run.actions.embed[0][1])
        self.assertIn("Harbor", run.actions.embed[0][1])
        self.assertRegex(run.actions.embed[1][1], r"^#[0-9A-F]{6}$", "the target has no color, so it gets a random one")

    def test_every_block_is_in_the_reference(self):
        documented = set()
        for header, rows in tables(self.text):
            if header and header[0] == "Block":
                for row in rows:
                    for span in _SPAN.findall(row[0]):
                        if span.startswith("{"):
                            documented.add(re.split(r"[(:}]", span[1:], maxsplit=1)[0].lower())
        missing = [name for name in BLOCKS if name not in documented]
        self.assertEqual(missing, [])

    def test_every_property_is_in_the_reference(self):
        documented = {span for _, rows in tables(self.text) for row in rows for span in _SPAN.findall(row[0])}
        for name in (*_PERSON, *_CHANNEL, "owner", "random", "randomonline", "randomoffline", "members", "bots", "humans", "roles", "channels", "is_added", "has_elevated_perms",
                     *objects.server(self.scope.guild).properties, *objects.invite(None).properties):
            self.assertIn(f"({name})", documented, name)

    def test_every_number_format_is_in_the_reference(self):
        rows = [row for header, rows in tables(self.text) if header and header[0] == "Format" for row in rows]
        self.assertEqual([_SPAN.findall(row[0])[0] for row in rows], list(FORMATS))

    def test_the_greeting_values_are_in_the_reference(self):
        documented = {span for header, rows in tables(self.text) if header and header[0] == "Block" for row in rows for span in _SPAN.findall(row[0])}
        self.assertLessEqual({"{joined}", "{invite}"}, documented)

    def test_the_limits_match_the_code(self):
        rows = {row[0]: row[1] for header, rows in tables(self.text) if header == ["What", "Limit"] for row in rows}
        self.assertEqual(rows["Characters in a tag or trigger's text"], f"{TEMPLATE_MAX:,}")
        self.assertEqual(rows["Characters one block can produce"], f"{MAX_VALUE:,}")
        self.assertEqual(rows["Characters handled in one run"], f"{MAX_WORK:,}")
        self.assertEqual(rows["Blocks in one template"], f"{MAX_BLOCKS:,}")
        self.assertEqual(rows["Time for one run"], f"{MAX_SECONDS} seconds")
        self.assertEqual(rows["Command blocks that run per tag"], str(MAX_COMMANDS))
        self.assertEqual(rows["Emoji per tag, for each of react and reactu"], str(MAX_REACTIONS))
        self.assertEqual(rows["Characters in a math expression"], f"{MAX_LENGTH:,}")
        self.assertEqual(rows["How deep a math expression can nest"], f"{MAX_DEPTH} levels")
        self.assertEqual(rows["Size of a whole number in math"], f"{MAX_INT_BITS:,} bits")
