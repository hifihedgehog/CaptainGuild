"""Double-brace tags into the template language: each converted template is run for a member and read back, so what's checked is what a server would see."""
import datetime
import json
import random
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from captainguild.core import messages
from captainguild.core.invitewatch import Invite
from captainguild.core.tagscript import Scope, objects, render_payload, render_text
from captainguild.core.tagscript import doublebrace
from captainguild.core.tagscript.doublebrace import CLOSE_BRACE, OPEN_BRACE, convert_message, convert_text, date_codes
from captainguild.core.validate import Invalid
from tests.fakes import FakeAsset, FakeChannel, FakeGuild, FakeMember

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
CREATED = datetime.datetime(2015, 1, 1, tzinfo=UTC)
JOINED = datetime.datetime(2026, 11, 1, 9, 30, 0, tzinfo=UTC)
ICON = "https://cdn.example/icons/1234567890/icon.png"


class Server(FakeGuild):
    """A server whose member count is a number the test names, as a large server's is."""

    def __init__(self, name: str, count: int) -> None:
        super().__init__(name)
        self.count = count

    @property
    def member_count(self) -> int:
        return self.count


class Person(FakeMember):
    @property
    def display_name(self) -> str:
        return self.nick or self.global_name or self.name


def arabic(grouped: str) -> str:
    return "".join(chr(0x066C) if character == "," else chr(0x0660 + int(character)) for character in grouped)


class Case(unittest.TestCase):
    def setUp(self):
        self.guild = Server("Test Server", 1234)
        self.guild.id = 1234567890
        self.guild.icon = FakeAsset(ICON)
        self.member = Person(self.guild, "john.doe", created=CREATED, joined=JOINED)
        self.member.id = 1234567890
        self.member.mention = "<@1234567890>"
        self.member.global_name = "John Doe"
        self.member.discriminator = "1234"
        self.channel = FakeChannel(self.guild, "welcome")
        self.invite = Invite(code="UyUVCEcBU9", uses=2724, max_uses=10000, max_age=86400, channel_id=4455667788, created_at=datetime.datetime(2026, 8, 11, tzinfo=UTC),
                             expires_at=datetime.datetime(2026, 11, 16, tzinfo=UTC), inviter_id=5491, inviter_name="Grace", inviter_username="grace", inviter_global_name="Grace")

    def scope(self, **changes) -> Scope:
        values = {"guild": self.guild, "member": self.member, "channel": self.channel, "now": NOW.timestamp(), "extra": {"joined": "123456", "invite": objects.invite(self.invite)}}
        return Scope(**{**values, **changes})

    def shown(self, template: str, *, number_format: str = "plain", **changes) -> str:
        """What a member would read: the template converted, then run."""
        made, _ = convert_text(template, number_format=number_format)
        return render_text(made, self.scope(**changes))

    def made(self, template: str, **options) -> str:
        return convert_text(template, **options)[0]

    def notes(self, template: str) -> list[str]:
        return convert_text(template)[1]


class EveryTagTest(Case):
    """Each row of the other syntax's reference page: its tag, and what the page says it shows."""

    def test_the_person(self):
        for tag, expected in [
            ("{{User.ID}}", "1234567890"), ("{{User.Name}}", "John Doe"), ("{{User.Username}}", "john.doe"), ("{{User.Discriminator}}", "1234"), ("{{User.GlobalName}}", "John Doe"),
            ("{{User.Mention}}", "<@1234567890>"), ("{{User.CreatedAt}}", "<t:1420070400:R>"), ("{{User.JoinedAt}}", f"<t:{int(JOINED.timestamp())}:R>"),
            ("{{User.LeftAt}}", f"<t:{int(NOW.timestamp())}:R>"), ("{{User.Avatar}}", "https://cdn.example/avatar.png"), ("{{User.Bot}}", "false"), ("{{User.Pending}}", "false"),
        ]:
            self.assertEqual(self.shown(tag), expected, tag)
            self.assertEqual(self.notes(tag), [], tag)

    def test_a_bot_on_the_rules_screen(self):
        robot = Person(self.guild, "robot", bot=True, pending=True)
        self.assertEqual(self.shown("{{User.Bot}} {{User.Pending}}", member=robot), "true true")

    def test_a_name_without_a_global_name_is_the_username(self):
        self.member.global_name = None
        self.assertEqual(self.shown("[{{User.Name}}] [{{User.GlobalName}}]"), "[john.doe#1234] []", "an account that still has its old tag shows it, as it did")
        self.member.discriminator = "0"
        self.assertEqual(self.shown("[{{User.Name}}] [{{User.GlobalName}}]"), "[john.doe] []")

    def test_the_server(self):
        for tag, expected in [("{{Guild.ID}}", "1234567890"), ("{{Guild.Name}}", "Test Server"), ("{{Guild.Icon}}", ICON), ("[{{Guild.Splash}}]", "[]"), ("{{Guild.Members}}", "1234"),
                              ("{{Guild.MembersJoined}}", "123456"), ("[{{Guild.Banner}}]", "[]")]:
            self.assertEqual(self.shown(tag), expected, tag)
            self.assertEqual(self.notes(tag), [], tag)
        self.guild.splash, self.guild.banner = FakeAsset("https://cdn.example/splash.png"), FakeAsset("https://cdn.example/banner.png")
        self.assertEqual(self.shown("{{Guild.Splash}} {{Guild.Banner}}"), "https://cdn.example/splash.png https://cdn.example/banner.png")

    def test_the_invite(self):
        for tag, expected in [
            ("{{Invite.Code}}", "UyUVCEcBU9"), ("{{Invite.Uses}}", "2724"), ("{{Invite.Inviter}}", "Grace"), ("{{Invite.ChannelID}}", "4455667788"),
            ("{{Invite.CreatedAt}}", f"<t:{int(self.invite.created_at.timestamp())}:R>"), ("{{Invite.ExpiresAt}}", f"<t:{int(self.invite.expires_at.timestamp())}:R>"),
            ("{{Invite.MaxAge}}", "86400"), ("{{Invite.MaxUses}}", "10000"), ("{{Invite.Temporary}}", "false"),
            ("{{Invite.Inviter.Name}}", "Grace"), ("{{Invite.Inviter.Username}}", "grace"), ("{{Invite.Inviter.GlobalName}}", "Grace"), ("{{Invite.Inviter.Mention}}", "<@5491>"),
            ("{{Invite.Inviter.ID}}", "5491"),
        ]:
            self.assertEqual(self.shown(tag), expected, tag)
            self.assertEqual(self.notes(tag), [], tag)

    def test_an_invite_nobody_could_tell_reads_as_it_did(self):
        unknown = {"joined": "1", "invite": objects.invite(None)}
        self.assertEqual(self.shown("{{Invite.Code}}|{{Invite.Uses}}|{{Invite.MaxUses}}|{{Invite.MaxAge}}|{{Invite.Temporary}}|{{Invite.ChannelID}}|{{Invite.Inviter}}|{{Invite.Inviter.Mention}}", extra=unknown),
                         "Unknown|0|0|0|false|0|Unknown|Unknown")

    def test_the_link_to_a_web_check_stays_and_is_named(self):
        made, notes = convert_text("Verify at {{Gate.Link}} please")
        self.assertEqual(made, "Verify at {{Gate.Link}} please")
        self.assertEqual(notes, ["`{{Gate.Link}}` has no match in the template language, so it stays as written."])
        self.assertEqual(render_text(made, self.scope()), "Verify at {{Gate.Link}} please")

    def test_ordinal_and_the_servers_number_format(self):
        self.assertEqual(self.shown("{{Ordinal(Guild.Members)}}"), "1234th")
        self.assertEqual(self.shown("{{Ordinal(Guild.MembersJoined)}}"), "123456th")
        for number_format, expected in [("plain", "1234th"), ("default", "1234th"), ("commas", "1,234th"), ("dots", "1.234th"), ("indian", "1,234th"), ("arabic", arabic("1,234") + "th")]:
            self.assertEqual(self.shown("{{Ordinal(Guild.Members)}}", number_format=number_format), expected, number_format)
        for locale, expected in [("default", "1234th"), ("commas", "1,234th"), ("dots", "1.234th"), ("indian", "1,234th"), ("arabic", arabic("1,234") + "th"), ("Commas", "1,234th"),
                                 (" DOTS ", "1.234th")]:
            self.assertEqual(self.shown('{{Ordinal(Guild.Members, "%s")}}' % locale, number_format="dots"), expected, locale)

    def test_ordinal_endings(self):
        for count, expected in [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (111, "111th"), (7600, "7600th"), (43461, "43461st")]:
            self.guild.count = count
            self.assertEqual(self.shown("{{Ordinal(Guild.Members)}}"), expected)

    def test_format_number_in_every_locale(self):
        for locale, expected in [("default", "123456"), ("dots", "123.456"), ("commas", "123,456"), ("indian", "1,23,456"), ("arabic", arabic("123,456"))]:
            self.assertEqual(self.shown('{{FormatNumber(Guild.MembersJoined, "%s")}}' % locale), expected, locale)
            self.assertEqual(self.shown("{{FormatNumber(Guild.MembersJoined)}}", number_format=locale), expected, locale)
        self.assertEqual(self.shown("{{FormatNumber(7600, 'dots')}}"), "7.600")

    def test_since_time_gives_words_that_stay(self):
        self.assertEqual(self.shown("{{SinceTime(User.CreatedAt)}}"), "11 years")
        self.assertEqual(self.shown("{{SinceTime(User.JoinedAt)}}"), "1 week")
        self.assertEqual(self.shown("{{SinceTime(Invite.CreatedAt)}}"), "3 months")
        self.assertEqual(self.shown("{{SinceTime(Invite.ExpiresAt)}}"), "4 days")

    def test_format_time_with_the_page_s_example(self):
        self.member.created_at = datetime.datetime(2025, 1, 1, tzinfo=UTC)
        self.assertEqual(self.shown('{{FormatTime(User.CreatedAt, "MMMM dd, yyyy")}}'), "January 01, 2025")

    def test_every_date_tag(self):
        self.member.created_at = datetime.datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
        self.member.joined_at = datetime.datetime(2025, 12, 25, 17, 45, 59, tzinfo=UTC)
        for tag, morning, evening in [
            ("yyyy", "2025", "2025"), ("yy", "25", "25"), ("MMMM", "March", "December"), ("MMM", "Mar", "Dec"), ("MM", "03", "12"), ("M", "3", "12"), ("dddd", "Tuesday", "Thursday"),
            ("ddd", "Tue", "Thu"), ("dd", "04", "25"), ("d", "4", "25"), ("HH", "05", "17"), ("hh", "05", "05"), ("h", "5", "5"), ("mm", "06", "45"), ("m", "6", "45"), ("ss", "07", "59"),
            ("s", "7", "59"),
        ]:
            self.assertEqual(self.shown('{{FormatTime(User.CreatedAt, "%s")}}' % tag), morning, tag)
            self.assertEqual(self.shown('{{FormatTime(User.JoinedAt, "%s")}}' % tag), evening, tag)

    def test_a_format_s_other_characters_stay(self):
        self.member.created_at = datetime.datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
        self.assertEqual(self.shown('{{FormatTime(User.CreatedAt, "dd/MM/yyyy, HH:mm:ss (100%)")}}'), "04/03/2025, 05:06:07 (100%)")
        self.assertEqual(self.shown('{{FormatTime(User.CreatedAt, "50%A in yyyy")}}'), "50%A in 2025", "a percent sign in the format is a percent sign, not the start of a code")
        self.assertEqual(date_codes("yyyy-MM-dd'T'HH:mm"), "%Y-%m-%d'T'%H:%M")
        self.assertEqual(date_codes("dddd d MMMM yyyy"), "%A %-d %B %Y")

    def test_the_time_someone_left_and_an_invite_s_times_format_too(self):
        self.assertEqual(self.shown('{{FormatTime(User.LeftAt, "yyyy-MM-dd HH:mm")}}'), "2026-11-11 12:00")
        self.assertEqual(self.shown('{{FormatTime(Invite.CreatedAt, "d MMM yyyy")}} to {{FormatTime(Invite.ExpiresAt, "d MMM yyyy")}}'), "11 Aug 2026 to 16 Nov 2026")

    def test_upper_lower_and_title(self):
        self.assertEqual(self.shown("{{Upper(User.Username)}}"), "JOHN.DOE")
        self.assertEqual(self.shown("{{Lower(User.Name)}}"), "john doe")
        self.assertEqual(self.shown("{{Title(User.Username)}}"), "John.Doe")
        self.assertEqual(self.shown('{{Upper("harbor")}} {{Lower("HARBOR")}} {{Title("harbor")}}'), "HARBOR harbor Harbor")

    def test_a_mention_a_name_and_an_ordinal_in_one_line_and_a_section_with_its_opposite(self):
        self.guild.count = 7600
        self.assertEqual(self.shown("{{User.Mention}} just landed in **{{Guild.Name}}** as its {{Ordinal(Guild.Members)}} arrival."), "<@1234567890> just landed in **Test Server** as its 7600th arrival.")
        both = "{{#User.Bot}}A machine was added.{{/User.Bot}}{{^User.Bot}}A person came in.{{/User.Bot}}"
        self.assertEqual(self.shown(both), "A person came in.")
        self.assertEqual(self.shown(both, member=Person(self.guild, "robot", bot=True)), "A machine was added.")

    def test_a_goodbye_and_a_line_break_between_tags(self):
        self.assertEqual(self.shown("{{User.Name}} is gone, which leaves {{Guild.Members}} of us."), "John Doe is gone, which leaves 1234 of us.")
        self.assertEqual(self.shown("Good to see {{User.Name}}\nin {{Guild.Name}}, the {{Ordinal(Guild.Members)}} to arrive."), "Good to see John Doe\nin Test Server, the 1234th to arrive.")


class NamesTest(Case):
    """A person's name in the other syntax is the one on their own profile: the global name, or the username when they set none. A server nickname
    never shows there, on a join or on a goodbye, so the converted tag reads the profile name and not {user}."""

    EVERY_FORM = ("{{User.Name}}", "{{User}}", "{{{User.Name}}}", "{{& User}}", "{{#User}}{{Name}}{{/User}}")

    def test_a_member_with_a_nickname_and_a_global_name(self):
        self.member.nick = "Johnny (he/him)"
        for tag in self.EVERY_FORM:
            self.assertEqual(self.shown(tag), "John Doe", tag)
        self.assertEqual(self.made("{{User.Name}} {{User}}"), "{user(profile_name)} {user(profile_name)}")
        self.assertEqual(self.shown("{{User.Name}} is gone, which leaves {{Guild.Members}} of us."), "John Doe is gone, which leaves 1234 of us.")
        self.assertEqual(self.shown("{{Upper(User.Name)}} {{Title(User.Name)}} {{User.GlobalName}} {{User.Username}}"), "JOHN DOE John Doe John Doe john.doe")
        self.assertEqual(render_text("{user}", self.scope()), "Johnny (he/him)", "the template language's own {user} is the name this server knows them by, which is why the tag doesn't become it")

    def test_a_member_with_neither(self):
        self.member.nick = self.member.global_name = None
        self.member.discriminator = "0"
        for tag in self.EVERY_FORM:
            self.assertEqual(self.shown(tag), "john.doe", tag)
        self.assertEqual(self.shown("[{{User.GlobalName}}] {{#User.GlobalName}}has one{{/User.GlobalName}}{{^User.GlobalName}}has none{{/User.GlobalName}}"), "[] has none")

    def test_a_member_with_a_nickname_and_no_global_name(self):
        self.member.nick, self.member.global_name, self.member.discriminator = "Johnny", None, "0"
        for tag in self.EVERY_FORM:
            self.assertEqual(self.shown(tag), "john.doe", tag)

    def test_an_account_that_still_has_its_old_tag(self):
        self.member.nick, self.member.global_name = "Johnny", None
        for tag in self.EVERY_FORM:
            self.assertEqual(self.shown(tag), "john.doe#1234", tag)
        self.member.global_name = "John Doe"
        self.assertEqual(self.shown("{{User.Name}}"), "John Doe", "a global name comes first")

    def test_someone_banned_who_is_only_a_user_now(self):
        banned = SimpleNamespace(id=5, name="jane.roe", global_name="Jane Roe", discriminator="0", display_name="Jane Roe", mention="<@5>", bot=False, created_at=CREATED,
                                 display_avatar=FakeAsset(), avatar=FakeAsset())
        self.assertEqual(self.shown("{{User.Name}} ({{User.Username}}) was banned. [{{User.JoinedAt}}]", member=banned), "Jane Roe (jane.roe) was banned. []")

    def test_the_avatar_is_the_one_on_their_profile_too(self):
        self.member.display_avatar = FakeAsset("https://cdn.example/in-this-server.png")
        self.assertEqual(self.made("{{User.Avatar}}"), "{user(profile_avatar)}")
        self.assertEqual(self.shown("{{User.Avatar}}"), "https://cdn.example/avatar.png")

    def test_the_inviter_is_named_the_same_way(self):
        named = Invite(code="x", inviter_id=7, inviter_name="Grace Hopper", inviter_username="grace", inviter_global_name="Grace Hopper")
        plain = Invite(code="x", inviter_id=7, inviter_name="grace", inviter_username="grace", inviter_global_name="")
        tagged = Invite(code="x", inviter_id=7, inviter_name="grace#5491", inviter_username="grace", inviter_global_name="")
        template = "{{Invite.Inviter}}|{{Invite.Inviter.Name}}|{{Invite.Inviter.Username}}|[{{Invite.Inviter.GlobalName}}]|{{#Invite.Inviter}}{{Name}}{{/Invite.Inviter}}"
        self.assertEqual(self.made(template), "{invite(inviter)}|{invite(inviter)}|{invite(inviter_username)}|[{invite(inviter_global_name)}]|{invite(inviter)}")
        for invite, expected in [(named, "Grace Hopper|Grace Hopper|grace|[Grace Hopper]|Grace Hopper"), (plain, "grace|grace|grace|[]|grace"), (tagged, "grace#5491|grace#5491|grace|[]|grace#5491")]:
            self.assertEqual(self.shown(template, extra={"joined": "1", "invite": objects.invite(invite)}), expected)


class StoredTemplatesTest(Case):
    """The three templates a real server stored: its welcome embed, its picture text and its join DM. Only the names are stand-ins."""

    def setUp(self):
        super().setUp()
        self.guild.name = "Harbor"
        self.guild.count = 43461

    def test_the_welcome_embed(self):
        stored = '{"embeds": [{"title": "Welcome {{User.Name}}", "thumbnail": {"url": "{{Guild.Icon}}"}, "description": "Glad to have you in {{Guild.Name}}! You make the {{Ordinal(Guild.Members)}} person aboard"}]}'
        made, notes = convert_message(stored)
        self.assertEqual(notes, [])
        self.assertEqual(made, {"content": "", "buttons": [], "embeds": [{
            "title": "Welcome {user(profile_name)}", "thumbnail": {"url": "{server(icon)}"}, "description": "Glad to have you in {server}! You make the {ord(plain):{server(members)}} person aboard"}]})
        self.assertEqual(made, messages.clean_payload(made, templates=True), "a greeting stores it as it is")
        [embed] = render_payload(made, self.scope())["embeds"]
        self.assertEqual(embed, {"title": "Welcome John Doe", "thumbnail": {"url": ICON}, "description": "Glad to have you in Harbor! You make the 43461st person aboard"})

    def test_the_picture_text(self):
        stored = "Ahoy {{User.Name}}\r\nyou reached {{Guild.Name}}!\r\nThe {{Ordinal(Guild.Members)}} berth is yours."
        made, notes = convert_text(stored)
        self.assertEqual(notes, [])
        self.assertEqual(made, "Ahoy {user(profile_name)}\r\nyou reached {server}!\r\nThe {ord(plain):{server(members)}} berth is yours.")
        self.assertEqual(render_text(made, self.scope()), "Ahoy John Doe\r\nyou reached Harbor!\r\nThe 43461st berth is yours.")

    def test_the_join_dm(self):
        made, notes = convert_message('{"content": "Ahoy {{User.Mention}}, this is **{{Guild.Name}}**! Yours is the **{{Ordinal(Guild.Members)}}** berth!"}')
        self.assertEqual(notes, [])
        self.assertEqual(made, {"content": "Ahoy {mention}, this is **{server}**! Yours is the **{ord(plain):{server(members)}}** berth!", "embeds": [], "buttons": []})
        self.assertEqual(render_payload(made, self.scope())["content"], "Ahoy <@1234567890>, this is **Harbor**! Yours is the **43461st** berth!")


class GrammarTest(Case):
    def test_spaces_inside_a_tag_and_both_raw_forms(self):
        self.assertEqual(self.made("{{ User.Name }} {{{User.Name}}} {{{ User.Name }}} {{& User.Name}} {{&User.Name}}"), " ".join(["{user(profile_name)}"] * 5))

    def test_a_comment_shows_nothing_and_is_noted(self):
        made, notes = convert_text("a{{! not for members }}b")
        self.assertEqual((made, notes), ("ab", ["The comment `{{! not for members }}` was left out. It never showed in a message."]))

    def test_inside_sections_inside_sections_the_outer_thing_s_name_wins(self):
        # The reference fills its names from the innermost context out, each overwriting the one before, so the outermost thing that has a
        # name gives it, and a top-level name beats them all.
        self.assertEqual(self.shown("{{#User}}{{#Guild}}{{Name}} {{ID}} {{Members}}{{/Guild}}{{/User}}"), "John Doe 1234567890 1234")
        self.assertEqual(self.shown("{{#Guild}}{{#User}}{{Name}} {{Username}}{{/User}}{{/Guild}}"), "Test Server john.doe")
        self.assertEqual(self.shown("{{#Invite.Inviter}}{{#User}}{{Name}} {{Mention}}{{/User}}{{/Invite.Inviter}}"), "Grace <@5491>")
        self.assertEqual(self.shown("{{#User}}{{#Invite}}{{Code}} {{Name}} {{Guild}}{{/Invite}}{{/User}}"), "UyUVCEcBU9 John Doe Test Server")

    def test_new_tag_marks(self):
        self.assertEqual(self.shown("{{=<% %>=}}<% User.Name %> {{User.Name}} <%={{ }}=%>{{Guild.Name}} <%Guild.Name%>"), "John Doe {{User.Name}} Test Server <%Guild.Name%>")

    def test_sections_on_a_flag(self):
        self.assertEqual(self.made("{{#User.Bot}}machine{{/User.Bot}}{{^User.Bot}}person{{/User.Bot}}"), "{if({user(bot)}==true):machine}{if({user(bot)}!=true):person}")
        self.assertEqual(self.shown("{{#User.Bot}}machine{{/User.Bot}}{{^User.Bot}}person{{/User.Bot}}"), "person")
        self.assertEqual(self.shown("{{#User.Pending}}wait{{/User.Pending}}{{^User.Pending}}in{{/User.Pending}}", member=Person(self.guild, "new", pending=True)), "wait")

    def test_a_section_shows_for_a_number_that_is_not_zero_and_text_that_is_not_empty(self):
        template = "{{#Invite.MaxUses}}limited{{/Invite.MaxUses}}{{^Invite.MaxUses}}open{{/Invite.MaxUses}} {{#User.GlobalName}}named{{/User.GlobalName}}{{^User.GlobalName}}plain{{/User.GlobalName}}"
        self.assertEqual(self.shown(template), "limited named")
        self.member.global_name = None
        self.assertEqual(self.shown(template, extra={"invite": objects.invite(Invite(code="x"))}), "open plain")
        self.assertEqual(self.shown("{{#Guild.Splash}}has one{{/Guild.Splash}}{{^Guild.Splash}}has none{{/Guild.Splash}}"), "has none")

    def test_a_section_on_a_time_shows_when_there_is_one(self):
        template = "{{#Invite.ExpiresAt}}ends{{/Invite.ExpiresAt}}{{^Invite.ExpiresAt}}stays{{/Invite.ExpiresAt}}"
        self.assertEqual(self.shown(template), "ends")
        self.assertEqual(self.shown(template, extra={"invite": objects.invite(Invite(code="x"))}), "stays")

    def test_a_section_on_a_whole_thing_reads_its_fields_by_their_own_names(self):
        self.assertEqual(self.shown("{{#User}}{{Name}} ({{ID}}){{/User}} {{#Guild}}{{Name}} has {{Members}}{{/Guild}} {{#Invite}}{{Code}} by {{Inviter.Mention}}{{/Invite}}"),
                         "John Doe (1234567890) Test Server has 1234 UyUVCEcBU9 by <@5491>")
        self.assertEqual(self.shown("{{#Invite.Inviter}}{{Name}} {{ID}}{{/Invite.Inviter}}"), "Grace 5491")
        self.assertEqual(self.shown("{{#User}}{{Guild.Name}}{{/User}}"), "Test Server", "names from outside the section still read")

    def test_a_section_that_can_never_show_is_left_out_with_a_note(self):
        made, notes = convert_text("a{{^User}}never{{/User}}b")
        self.assertEqual((made, notes), ("ab", ["`{{^User}}` is a section that never shows, so its text was left out."]))

    def test_sections_inside_sections(self):
        template = "{{#User.Bot}}{{#User.Pending}}both{{/User.Pending}}{{^User.Pending}}bot{{/User.Pending}}{{/User.Bot}}{{^User.Bot}}{{#User.Pending}}pending{{/User.Pending}}{{^User.Pending}}member{{/User.Pending}}{{/User.Bot}}"
        for bot, pending, expected in [(True, True, "both"), (True, False, "bot"), (False, True, "pending"), (False, False, "member")]:
            self.assertEqual(self.shown(template, member=Person(self.guild, "x", bot=bot, pending=pending)), expected)

    def test_a_section_tag_alone_on_its_line_takes_the_line(self):
        template = "Hi\n{{#User.Bot}}\n  beep\n{{/User.Bot}}\n{{^User.Bot}}\n  hello\n{{/User.Bot}}\nbye"
        self.assertEqual(self.made(template), "Hi\n{if({user(bot)}==true):  beep\n}{if({user(bot)}!=true):  hello\n}bye")
        self.assertEqual(self.shown(template), "Hi\n  hello\nbye")
        self.assertEqual(self.shown("  {{#User.Bot}}  \r\nbeep\r\n\t{{/User.Bot}}\r\n{{! note }}\r\nend", member=Person(self.guild, "x", bot=True)), "beep\r\nend")

    # The same text as a message and as picture text. The other bot rendered a message's stored JSON, where each line break is written as a
    # backslash and an n, so no tag there stood alone on a line. Its picture text was plain text, where they did. These are what it showed.
    LINES = "Hi {{User.Mention}}!\n{{#User.Bot}}\nA machine.\n{{/User.Bot}}\n{{! note for staff }}\nSee you around."
    AS_A_MESSAGE = {False: "Hi <@1234567890>!\n\n\nSee you around.", True: "Hi <@1234567890>!\n\nA machine.\n\n\nSee you around."}
    AS_PICTURE_TEXT = {False: "Hi <@1234567890>!\nSee you around.", True: "Hi <@1234567890>!\nA machine.\nSee you around."}

    def person(self, bot: bool) -> Person:
        someone = Person(self.guild, "x", bot=bot)
        someone.mention = "<@1234567890>"
        return someone

    def test_in_a_message_a_tag_alone_on_its_line_keeps_the_line_breaks_around_it(self):
        made, notes = convert_message({"content": self.LINES})
        for bot, expected in self.AS_A_MESSAGE.items():
            self.assertEqual(render_payload(made, self.scope(member=self.person(bot)))["content"], expected, bot)
        made, _ = convert_text(self.LINES, message=True)
        self.assertEqual(made, "Hi {mention}!\n{if({user(bot)}==true):\nA machine.\n}\n\nSee you around.")

    def test_in_picture_text_a_tag_alone_on_its_line_takes_the_line(self):
        made, _ = convert_text(self.LINES)
        for bot, expected in self.AS_PICTURE_TEXT.items():
            self.assertEqual(render_text(made, self.scope(member=self.person(bot))), expected, bot)

    def test_a_tag_with_text_beside_it_keeps_its_line(self):
        self.assertEqual(self.shown("a {{^User.Bot}}\nb\n{{/User.Bot}} c"), "a \nb\n c")
        self.assertEqual(self.made("{{User.Name}}\n{{Guild.Name}}\n"), "{user(profile_name)}\n{server}\n", "a value's line stays whole")
        self.assertEqual(self.made("a\n  {{! gone }}"), "a\n", "a tag that ends the text takes its line")
        self.assertEqual(self.made("a\n  {{! gone }}  "), "a\n    ", "spaces after the text's last tag keep it from taking the line")
        self.assertEqual(self.made("a\n  {{! gone }}  \nb"), "a\nb")

    def test_comparisons(self):
        self.guild.count = 100
        for expression, expected in [("Guild.Members == 100", "true"), ("Guild.Members != 100", "false"), ("Guild.Members > 99", "true"), ("Guild.Members >= 101", "false"),
                                     ("Guild.Members < 100", "false"), ("Guild.Members <= 100", "true"), ('User.Username == "john.doe"', "true"), ('User.Name != "John Doe"', "false"),
                                     ("User.Bot == false", "true"), ("User.Bot == true", "false"), ("100 == Guild.Members", "true"), ('User.GlobalName == ""', "false")]:
            self.assertEqual(self.shown("{{%s}}" % expression), expected, expression)
            self.assertEqual(self.shown("{{#%s}}yes{{/%s}}{{^%s}}no{{/%s}}" % ((expression,) * 4)), "yes" if expected == "true" else "no", expression)

    def test_and_or_and_not(self):
        self.guild.count = 100
        for expression, expected in [("User.Bot && User.Pending", "no"), ("!User.Bot && !User.Pending", "yes"), ("User.Bot || Guild.Members > 50", "yes"), ("User.Bot || User.Pending", "no"),
                                     ("!(User.Bot || User.Pending)", "yes"), ("!(Guild.Members > 50)", "no"), ("(User.Bot || Guild.Members > 50) && !User.Pending", "yes"),
                                     ("User.Bot || (Guild.Members > 50 && User.Pending)", "no"), ("!User.Bot && (User.Pending || Guild.Members == 100)", "yes"),
                                     ("!(!User.Bot && Guild.Members == 100)", "no"), ("!!User.Bot", "no"), ("!(User.Bot || User.Pending) && Guild.Members == 100", "yes"),
                                     ("!(User.Bot && User.Pending) || User.Bot", "yes"), ("!(Guild.Members > 50 || User.Bot) || User.Pending", "no"),
                                     ("!(User.Bot || User.Pending) && !(Guild.Members < 50 && User.Bot)", "yes")]:
            self.assertEqual(self.shown("{{#%s}}yes{{/%s}}{{^%s}}no{{/%s}}" % ((expression,) * 4)), expected, expression)
            # Parentheses around it, because a tag that opens with ! is a comment.
            self.assertEqual(self.shown("{{(%s)}}" % expression), "true" if expected == "yes" else "false", expression)
        self.assertEqual(self.made("{{#User.Bot && User.Pending}}x{{/User.Bot && User.Pending}}"), "{all({user(bot)}==true|{user(pending)}==true):x}")
        self.assertEqual(self.made("{{^User.Bot || User.Pending}}x{{/User.Bot || User.Pending}}"), "{any({user(bot)}==true|{user(pending)}==true):|x}")
        self.assertEqual(self.made("{{#!User.Bot}}x{{/!User.Bot}}{{^!User.Bot}}y{{/!User.Bot}}"), "{if({user(bot)}!=true):x}{if({user(bot)}==true):y}")

    def test_a_tag_that_opens_with_an_exclamation_mark_is_a_comment(self):
        made, notes = convert_text("[{{!User.Bot}}]")
        self.assertEqual(made, "[]")
        self.assertIn("The comment", notes[0])

    def test_arithmetic(self):
        self.guild.count = 100
        for expression, expected in [("Guild.Members + 1", "101"), ("Guild.Members - 1", "99"), ("Guild.Members * 2", "200"), ("Guild.Members % 7", "2"), ("-Guild.Members", "-100"),
                                     ("Guild.Members + Guild.MembersJoined", "123556"), ("(Guild.Members + 1) * 2", "202"), ("Guild.Members - 1 - 1", "98"), ("2 + 3 * 4", "14")]:
            self.assertEqual(self.shown("{{%s}}" % expression), expected, expression)
            self.assertEqual(self.notes("{{%s}}" % expression), [], expression)
        self.assertEqual(self.shown("{{Ordinal(Guild.Members + 1)}} {{FormatNumber(Guild.Members * 1000, 'commas')}}"), "101st 100,000")
        self.assertEqual(self.shown("{{#Guild.Members + 1 > 100}}over{{/Guild.Members + 1 > 100}}"), "over")

    def test_a_division_is_worked_out_and_named(self):
        self.guild.count = 100
        made, notes = convert_text("{{Guild.Members / 8}} {{2 ** 3}}")
        self.assertEqual(render_text(made, self.scope()), "12.5 8.0")
        self.assertEqual(notes, ["`{{Guild.Members / 8}}` is worked out by the math block, which writes this kind of result with a decimal point, so 8/2 shows as 4.0.",
                                 "`{{2 ** 3}}` is worked out by the math block, which writes this kind of result with a decimal point, so 8/2 shows as 4.0."])

    def test_a_choice(self):
        for bot, expected in [(True, "beep"), (False, "hello John Doe")]:
            self.member.bot = bot
            self.assertEqual(self.shown('{{User.Bot ? "beep" : "hello " + User.Name}}'), expected)
        self.guild.count = 5
        self.assertEqual(self.shown('{{Guild.Members > 100 ? "big" : Guild.Members > 10 ? "mid" : "small"}}'), "small")
        self.assertEqual(self.shown('{{User.Bot ? "only bots see this"}}|'), "|")
        self.assertEqual(self.shown("{{User.Bot ? Guild.Members : Guild.MembersJoined}}"), "123456")

    def test_text_in_quotes_and_joined_text(self):
        self.assertEqual(self.shown("""{{"double"}} {{'single'}} {{"a\\"b"}} {{"Hello, " + User.Name + "!"}} {{"#" + Guild.Members}}"""),
                         """double single a"b Hello, John Doe! #1234""")

    def test_functions_inside_functions(self):
        self.assertEqual(self.made("{{Upper(Title(Lower(Guild.Name)))}}"), "{upper:{title:{lower:{server}}}}")
        self.assertEqual(self.shown('{{Upper("you are the " + Ordinal(Guild.Members, "commas") + " of " + FormatNumber(Guild.MembersJoined, "dots"))}}'), "YOU ARE THE 1,234TH OF 123.456")
        self.assertEqual(self.shown('{{Title(User.Bot ? "a bot" : "a person")}}'), "A Person")

    def test_whole_things_print_their_names(self):
        self.assertEqual(self.shown("{{User}} {{Guild}} {{Invite.Inviter}}"), "John Doe Test Server Grace")

    def test_html_entities_in_the_text_become_their_characters(self):
        self.assertEqual(self.made("Tom &amp; Jerry &lt;3 &#39;{{User.Name}}&#39; &quot;x&quot;"), "Tom & Jerry <3 '{user(profile_name)}' \"x\"")


class OwnVariablesTest(Case):
    """Text holding a pipe inside a section goes through variables the converter names. Those names never meet the person's own braces."""

    def test_the_person_s_own_braces_that_read_like_those_names_stay_as_written(self):
        template = "{{#User.Bot}}a|b{{/User.Bot}} and {text1} and {text2} and {TEXT3}"
        self.assertEqual(self.shown(template), "and {text1} and {text2} and {TEXT3}")
        self.assertEqual(self.shown(template, member=Person(self.guild, "robot", bot=True)), "a|b and {text1} and {text2} and {TEXT3}")
        self.assertEqual(self.shown("{text9}{{User.Bot ? 'x|y' : 'z'}}{text10}"), "{text9}z{text10}")

    def test_across_a_message_s_parts_too(self):
        made, _ = convert_message({"content": "{{#User.Bot}}a|b{{/User.Bot}}hello", "embeds": [{"description": "keep {text1} and {text2}"}]})
        shown = render_payload(made, self.scope())
        self.assertEqual((shown["content"], shown["embeds"][0]["description"]), ("hello", "keep {text1} and {text2}"))

    def test_a_number_past_any_the_converter_reaches_is_passed_over(self):
        # Python reads at most 4,300 digits as a number. The converter's own count never gets near 19 digits, so a longer run can't meet it.
        for data in ({"content": "hi", "note": "text" + "9" * 4400}, {"embeds": [{"title": "t", "image": {"url": "text" + "1" * 4400}}]},
                     {"content": "{{#User.Bot}}a|b{{/User.Bot}}{text" + "7" * 19 + "}"}):
            made, _ = convert_message(data)
            self.assertIsInstance(made, dict)
        self.assertEqual(self.shown("{{#User.Bot}}a|b{{/User.Bot}}{text" + "7" * 19 + "}{text" + "9" * 18 + "}"), "{text" + "7" * 19 + "}{text" + "9" * 18 + "}")
        self.assertIn("{=(text" + str(10 ** 18) + "):", self.made("{{#User.Bot}}a|b{{/User.Bot}}{text" + "9" * 18 + "}"), "18 digits are read, and its own names go past them")
        self.assertIn("{=(text1):", self.made("{{#User.Bot}}a|b{{/User.Bot}}{text" + "7" * 19 + "}"), "19 are passed over")


class StaysPlainTest(Case):
    """Text the other syntax showed as it was written has to show the same way here, whatever it looks like to the template language."""

    def same(self, template: str, expected: str, **changes) -> str:
        made = self.made(template)
        self.assertEqual(render_text(made, self.scope(**changes)), expected, made)
        return made

    def test_single_braces_that_would_read_as_a_block(self):
        made = self.same("Type {user} or {server(members)} or {mention}, {{User.Name}}", "Type {user} or {server(members)} or {mention}, John Doe")
        self.assertEqual(made, f"Type {OPEN_BRACE}user{CLOSE_BRACE} or {OPEN_BRACE}server(members){CLOSE_BRACE} or {OPEN_BRACE}mention{CLOSE_BRACE}, {{user(profile_name)}}")
        self.assertEqual(self.notes("Type {user}"), [doublebrace.BRACE_NOTE], "the person importing is told what those blocks are")
        self.assertEqual(render_text(f"[{OPEN_BRACE}][{CLOSE_BRACE}]", self.scope()), "[{][}]")
        self.same("{upper:shout} {m:1+1} {random:a,b} {delete}{embed(title):x} {unix} {1} {joined} {invite(code)} {if(1==1):yes|no}", "{upper:shout} {m:1+1} {random:a,b} {delete}{embed(title):x} {unix} {1} {joined} {invite(code)} {if(1==1):yes|no}")

    def test_single_braces_that_would_not_stay_as_they_were(self):
        for template in ("{nothing}", "{} { } {a b}", "a { b", "a } b", "} {", "{note: keep}", "JSON: {\"a\": 1}", "{.} {..}"):
            self.assertEqual(convert_text(template), (template, []), template)
            self.assertEqual(render_text(template, self.scope()), template, template)

    def test_braces_around_a_tag(self):
        self.same("{ {{User.Name}} }", "{ John Doe }")
        self.same("{upper:{{User.Name}}}", "{upper:John Doe}")
        self.same("{{User.Name}}} and {{{User.Name}}}} and {if({{Guild.Members}}==1234):yes|no}", "John Doe} and John Doe} and {if(1234==1234):yes|no}")

    def test_many_braces_around_tags_still_leave_an_ordinary_pair_alone(self):
        """Pairs with a tag inside are written out without asking the engine, so the pairs it's asked about are the ones that can stay as they are."""
        made = self.made("{ {{User.Name}} }" * (doublebrace.MAX_PROBES + 50) + "{nothing at all, really}")
        self.assertTrue(made.endswith("{nothing at all, really}"), made[-80:])
        self.assertEqual(made.count(OPEN_BRACE), doublebrace.MAX_PROBES + 50)

    def test_braces_inside_a_section(self):
        made = self.same("{{^User.Bot}}see {user}, } and { and {x}{{/User.Bot}}", "see {user}, } and { and {x}")
        self.assertIn(CLOSE_BRACE, made)
        self.same("{{^User.Bot}}}{{/User.Bot}}{{^User.Bot}}{ {{/User.Bot}}", "}{")

    def test_a_pipe_inside_a_section(self):
        made = self.same("{{^User.Bot}}rules | roles{{/User.Bot}}{{#User.Bot}}a|b|c{{/User.Bot}}", "rules | roles")
        self.assertEqual(made, "{=(text1):rules | roles}{=(text2):}{text{if({user(bot)}!=true):1|2}}{=(text3):a|b|c}{=(text4):}{text{if({user(bot)}==true):3|4}}")
        self.same("{{^User.Bot}}rules | roles{{/User.Bot}}{{#User.Bot}}a|b|c{{/User.Bot}}", "a|b|c", member=Person(self.guild, "robot", bot=True))

    def test_a_pipe_two_sections_deep_and_in_a_choice(self):
        template = "{{^User.Bot}}[{{#User.Pending}}wait | read{{/User.Pending}}{{^User.Pending}}in | out{{/User.Pending}}]{{/User.Bot}}"
        self.same(template, "[in | out]")
        self.same(template, "[wait | read]", member=Person(self.guild, "new", pending=True))
        self.same(template, "", member=Person(self.guild, "robot", bot=True))
        self.same('{{User.Bot ? "0|1" : "yes | no"}} {{^User.Bot}}{{Upper("a|b")}}{{/User.Bot}}', "yes | no A|B")

    def test_a_pipe_in_a_date_format_inside_a_section(self):
        self.member.created_at = datetime.datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
        self.same('{{^User.Bot}}{{FormatTime(User.CreatedAt, "HH|mm {s}")}}{{/User.Bot}}', "05|06 {7}")

    def test_colons_and_parentheses_inside_a_section(self):
        self.same("{{^User.Bot}}time: 10:30 (UTC) :) (a{{/User.Bot}}", "time: 10:30 (UTC) :) (a")
        self.same('{{User.Bot ? "x" : "a:b (c) d)"}}', "a:b (c) d)")

    def test_a_name_s_own_punctuation(self):
        self.member.global_name = "a|b {user} ](x) }:)"
        self.same("{{^User.Bot}}{{User.Name}}|{{Upper(User.Name)}}{{/User.Bot}}", "a|b {user} ]\\(x) }:)|A|B {USER} ]\\(X) }:)")
        self.same("{ {{User.Name}} }", "{ a|b {user} ]\\(x) }:) }")

    def test_an_unknown_tag_inside_a_section_stays_as_written(self):
        made, notes = convert_text("{{^User.Bot}}go to {{Gate.Link}} now{{/User.Bot}}")
        self.assertEqual(render_text(made, self.scope()), "go to {{Gate.Link}} now")
        self.assertEqual(notes, ["`{{Gate.Link}}` has no match in the template language, so it stays as written.", doublebrace.BRACE_NOTE])


class NoMatchTest(Case):
    def stays(self, template: str, reason: str) -> None:
        made, notes = convert_text(f"before {template} after")
        self.assertEqual(render_text(made, self.scope()), f"before {template} after", made)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(reason, notes[0])
        self.assertIn(f"`{template}`", notes[0], "the note names the tag")

    def test_names_nobody_has_heard_of(self):
        for template in ("{{Nope}}", "{{user.name}}", "{{User.Banner}}", "{{Guild.Owner}}", "{{Invite.Inviter.Avatar}}", "{{User.Name.First}}"):
            self.stays(template, "has no match in the template language")

    def test_a_whole_invite(self):
        self.stays("{{Invite}}", "names a whole invite")

    def test_what_the_engine_has_no_block_for(self):
        self.stays("{{Guild.Members in (1, 2)}}", "uses `in`")
        self.stays('{{User.Name =~ "^a"}}', "matches a pattern")
        self.stays("{{Guild.Members & 1}}", "works on bits")
        self.stays("{{~Guild.Members}}", "works on bits")
        self.stays("{{Guild.Members ?? 5}}", "`??`")
        self.stays("{{(1, 2, 3)}}", "a list in parentheses")
        self.stays("{{[User Name]}}", "isn't written in a way this reads")
        self.stays("{{Guild.Members +}}", "ends before it's finished")
        self.stays("{{Guild.Members 5}}", "where nothing fits")
        self.stays("{{(Guild.Members}}", "doesn't close its parentheses")
        self.stays("{{.}}", "isn't written in a way this reads")

    def test_functions_given_the_wrong_things(self):
        self.stays("{{Shout(User.Name)}}", "calls `Shout`")
        self.stays("{{upper(User.Name)}}", "calls `upper`")
        self.stays("{{Upper()}}", "gives `Upper` 0 values")
        self.stays("{{Upper(User.Name, User.Name)}}", "gives `Upper` 2 values")
        self.stays('{{Ordinal(Guild.Members, "roman")}}', "names a number format")
        self.stays("{{Ordinal(Guild.Members, User.Name)}}", "names a number format")
        self.stays("{{Ordinal()}}", "gives `Ordinal` 0 values")
        self.stays("{{SinceTime(Guild.Members)}}", "isn't one time")
        self.stays('{{FormatTime(User.Name, "yyyy")}}', "a time and a format in quotes")
        self.stays("{{FormatTime(User.CreatedAt, User.Name)}}", "a time and a format in quotes")
        self.stays("{{Upper(Invite)}}", "names a whole invite")

    def test_math_and_tests_on_the_wrong_things(self):
        self.stays("{{User.CreatedAt - 1}}", "does math with `-`")
        self.stays("{{User.Name * 2}}", "does math with `*`")
        self.stays("{{-User.Name}}", "a minus sign")
        self.stays("{{(!Guild.Members)}}", "isn't true or false")
        self.stays("{{User.Bot && Guild.Members}}", "isn't true or false")
        self.stays('{{Guild.Members ? "a" : "b"}}', "isn't true or false")
        self.stays('{{User.Name > "a"}}', "compares sizes")
        self.stays("{{User.CreatedAt == User.JoinedAt}}", "compares a time")
        for literal in ("a|b", "a)", "(a", "a==b", "a!b", "a<b", "{a}", " padded "):
            self.stays('{{User.Name == "%s"}}' % literal, "a condition can't hold")

    def test_a_section_that_has_no_match_keeps_its_tags_and_converts_what_is_inside(self):
        made, notes = convert_text("{{#Nope}}hi {{User.Name}}{{/Nope}}")
        self.assertEqual(render_text(made, self.scope()), "{{#Nope}}hi John Doe{{/Nope}}")
        self.assertEqual(notes, ["`{{#Nope}}` has no match in the template language, so the section's tags stay as written around its text."])
        made, notes = convert_text('x\n{{#"fixed"}}\nhi\n{{/"fixed"}}\ny')
        self.assertEqual(made, 'x\n{{#"fixed"}}\nhi\n{{/"fixed"}}\ny', "the lines the tags took come back with them")
        self.assertIn("makes a section out of fixed text", notes[0])

    def test_tags_that_are_not_whole(self):
        for template, reason in [("{{#User.Bot}}open", "opens a section that never closes"), ("shut{{/User.Bot}}", "closes a section that isn't open there"),
                                 ("{{#User.Bot}}a{{/User.Pending}}b", "closes a section that isn't open there"), ("{{> header}}", "pulls in another template"),
                                 ("{{}}", "An empty tag"), ("{{  }}", "An empty tag"), ("{{User.Name", "opens a tag that never closes"), ("{{{User.Name}}", "opens a tag that never closes"),
                                 ("{{=one=}}", "doesn't name two new tag marks"), ("{{{ }}}", "isn't a whole tag")]:
            made, notes = convert_text(f"a {template} z")
            self.assertEqual(made, f"a {template} z", template)
            self.assertEqual(render_text(made, self.scope()), f"a {template} z", template)
            self.assertTrue(any(reason in note for note in notes), (template, notes))

    def test_a_section_left_open_still_converts_what_follows(self):
        made, notes = convert_text("{{#User.Bot}}hello {{User.Name}}, {{^User.Pending}}welcome{{/User.Pending}}")
        self.assertEqual(render_text(made, self.scope()), "{{#User.Bot}}hello John Doe, welcome")
        self.assertEqual(len(notes), 1)

    def test_the_same_note_is_given_once(self):
        self.assertEqual(len(self.notes("{{Nope}} {{Nope}} {{Nope}}")), 1)
        self.assertEqual(len(self.notes("{{Nope}} {{Never}}")), 2)


class StricterFormsTest(Case):
    """Forms the other engine read more strictly than they look, as its source settles them: its == and != count values of two kinds as
    different, each of its functions takes one kind of value, text in quotes written as a date reads as a time there, and a message
    reached it as stored JSON."""

    def stays(self, template: str, reason: str, **options) -> None:
        made, notes = convert_text(f"before {template} after", **options)
        self.assertEqual(made, f"before {template} after")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(reason, notes[0])
        self.assertIn("`" + " ".join(template.split()) + "`", notes[0], "the note names the tag")

    def test_equal_and_not_equal_across_two_kinds_always_answer_the_same(self):
        for expression, expected in [("Guild.Members == '1234'", "false"), ("Guild.Members != '1234'", "true"), ("User.Discriminator == 1234", "false"),
                                     ("User.Bot == 'false'", "false"), ("1 != User.Bot", "true"), ("User.ID == '1234567890'", "false"),
                                     ("User.ID == 1234567890", "false"), ("Invite.Inviter.ID != 5491", "true"), ("Invite.ChannelID == '4455667788'", "false"),
                                     ("Guild.ID == User.Name", "false"), ("User.Pending || Guild.Members == '1234'", "false"), ("(!(Guild.Members == '1234'))", "true")]:
            self.assertEqual(self.shown("{{%s}}" % expression), expected, expression)
            self.assertEqual(self.shown("{{#%s}}yes{{/%s}}{{^%s}}no{{/%s}}" % ((expression,) * 4)), "yes" if expected == "true" else "no", expression)
        self.assertEqual(self.notes("{{Guild.Members == '1234'}}"),
                         ["`{{Guild.Members == '1234'}}` compares a number with text, two kinds of value the other bot never counted equal, so its `==` is always false."])
        self.assertEqual(self.notes("{{User.ID != 5}}"),
                         ["`{{User.ID != 5}}` compares an ID with a number, two kinds of value the other bot never counted equal, so its `!=` is always true."])
        self.assertEqual(len(self.notes("{{Guild.Members == '5' || Guild.Members / 2 > 1}}")), 2, "the math block's note comes as well")

    def test_a_time_or_a_whole_thing_in_a_comparison_still_stays_as_written(self):
        self.stays("{{User.JoinedAt == 'x'}}", "compares a time or a whole person")
        self.stays("{{User != 5}}", "compares a time or a whole person")

    def test_two_values_of_one_kind_still_compare(self):
        for expression, expected in [("User.ID == Guild.ID", "true"), ("User.ID != Invite.Inviter.ID", "true"), ("Ordinal(Guild.Members) == '1234th'", "true"),
                                     ("User.Name + '.' == 'John Doe.'", "true"), ("(Guild.Members > 5) == true", "true"), ("Guild.Members == Guild.MembersJoined", "false")]:
            self.assertEqual(self.shown("{{%s}}" % expression), expected, expression)
            self.assertEqual(self.notes("{{%s}}" % expression), [], expression)

    def test_a_choice_that_can_give_two_kinds_of_value_stays_as_written_in_a_comparison(self):
        for template in ("{{(User.Bot ? 1 : 'one') == 1}}", "{{(User.Bot ? 'a') == ''}}", "{{'x' != (User.Pending ? User.ID : 'x')}}"):
            self.stays(template, "a choice that can give two kinds of value")

    def test_a_section_on_a_choice_reads_its_kind(self):
        self.assertEqual(self.shown("[{{#User.Bot ? 1 : 0}}shown{{/User.Bot ? 1 : 0}}] [{{#User.Bot ? true : false}}shown{{/User.Bot ? true : false}}]"), "[] []")
        self.assertEqual(self.shown("[{{#User.Bot ? 0 : 1}}shown{{/User.Bot ? 0 : 1}}]"), "[shown]")

    def test_joining_and_adding_follow_the_kinds(self):
        self.assertEqual(self.shown("{{User.ID + '!'}} {{'#' + User.Bot}} {{(User.Bot ? 'a' : 1) + '?'}}"), "1234567890! #false 1?")
        self.stays("{{User.ID + 1}}", "does math with `+`")
        self.stays("{{User.ID + Guild.ID}}", "does math with `+`")
        self.stays("{{(User.Bot ? 1 : 2) + 1}}", "does math with `+`")
        self.stays("{{(User.Bot ? 'a') + 'b'}}", "`<nil>`")

    def test_each_function_takes_the_kind_of_value_the_other_bot_s_took(self):
        for template, reason in [("{{Ordinal(User.ID)}}", "gives `Ordinal` an ID, and the other bot's `Ordinal` takes only a number"),
                                 ("{{FormatNumber('7600')}}", "gives `FormatNumber` text, and the other bot's `FormatNumber` takes only a number"),
                                 ("{{Ordinal(User.Bot ? 1 : 'one')}}", "gives `Ordinal` a choice that can give two kinds of value"),
                                 ("{{Upper(Guild.Members)}}", "gives `Upper` a number, and the other bot's `Upper` takes only text"),
                                 ("{{Lower(User.ID)}}", "gives `Lower` an ID"), ("{{Title(User.Bot)}}", "gives `Title` a true-or-false value"),
                                 ("{{Upper(User.JoinedAt)}}", "gives `Upper` a time"), ("{{Upper(User)}}", "gives `Upper` a whole person, server or invite"),
                                 ("{{Upper(User.Bot ? 'a')}}", "gives `Upper` a choice with no `:` part")]:
            self.stays(template, reason)
        self.assertEqual(self.shown("{{Ordinal(User.Bot ? 1 : 2)}} {{Upper(User.Bot ? 'a' : 'b')}}"), "2nd B")

    def test_text_in_quotes_written_as_a_date_or_a_time_stays_as_written(self):
        for template in ("{{Guild.Members == '2024-01-01'}}", '{{User.Name == "2024-01-01 10:30"}}', "{{'3:04PM'}}", "{{'mon  JAN  2 15:04:05 2006'}}",
                         "{{Upper('2026-11-11T12:00:00Z')}}", "{{Guild.Members > '2024-01-01'}}", "{{User.Bot > '2024-01-01'}}", "{{'2024-01-01' == '2024-01-01'}}"):
            self.stays(template, "written as a date or a time")
        made, notes = convert_text("{{#'2024-01-01'}}x{{/'2024-01-01'}}")
        self.assertEqual(made, "{{#'2024-01-01'}}x{{/'2024-01-01'}}")
        self.assertIn("written as a date or a time", notes[0])
        self.assertEqual(self.shown("{{User.Bot == '2024-01-01'}} {{User.ID != '2024-01-01'}}"), "false true", "a time never equals true or false, or an ID")
        self.assertEqual(self.shown("{{'2024'}} {{'24-01-01'}} {{'10:30'}} {{'Monday January'}}"), "2024 24-01-01 10:30 Monday January", "never read as a time")

    def test_in_picture_text_either_quote_mark_ends_text_in_quotes_and_a_backslash_keeps_the_next_character(self):
        self.assertEqual(self.shown("{{'C:\\temp\\new'}} {{\"a\\\"b\"}} {{'abc\"}}"), 'C:tempnew a"b abc')
        self.stays("{{\"it's\"}}", "isn't written in a way this reads")
        self.stays("{{'say \"hi\"'}}", "where nothing fits")

    def test_in_a_message_text_in_quotes_reads_as_the_stored_json_reached_the_other_bot(self):
        # The stored JSON put a backslash before each double quote and each backslash, and the other engine's own escape took it off again.
        # So in a message a backslash in quotes is a plain one, a double quote inside single quotes is plain too, and a double quote that
        # opens text left a backslash the engine couldn't read.
        made, notes = convert_message({"content": "{{'C:\\temp\\new'}} {{'say \"hi\"'}} {{\"two\" + 'one'}}"})
        self.assertEqual(render_payload(made, self.scope())["content"], 'C:\\temp\\new say "hi" twoone')
        self.assertEqual(notes, ["Message text: `{{\"two\" + 'one'}}` holds text in double quotes. The other bot stored a message with a backslash before each double quote "
                                 "and couldn't read the tag, so the message didn't go out whenever it came to this tag."])
        made, notes = convert_text("{{'C:\\temp'}} {{User.Name == \"John Doe\"}}", message=True)
        self.assertEqual((render_text(made, self.scope()), len(notes)), ("C:\\temp true", 1))
        made, notes = convert_message({"content": "{{'it\\'s'}}"})
        self.assertEqual(made["content"], "{{'it\\'s'}}", "the other bot's text in quotes ended at the second quote mark")
        self.assertIn("isn't written in a way this reads", notes[0])

    def test_in_a_message_a_tag_holding_a_line_break_or_a_tab_stays_as_written(self):
        for template in ("{{User.Name\n== 'x'}}", "{{User.Name ==\t'x'}}", "{{'a\nb'}}", "{{#User.Bot\n}}x{{/User.Bot}}"):
            made, notes = convert_message({"content": f"before {template} after"})
            self.assertEqual(made["content"], f"before {template} after", template)
            self.assertEqual(len(notes), 1, notes)
            self.assertIn("holds a line break or a tab", notes[0], template)
        self.assertEqual(self.shown("{{User.Name\n== 'John Doe'}}"), "true", "picture text reads one as a space")


class GrownPartsTest(Case):
    """A converted tag is longer than the one it replaces, so a part inside its limit as pasted can pass it once converted. The message still
    comes back whole, with a note on how far over each part is, and Save refuses it until the person shortens it in the editor."""

    NAME = "{{User.Name}}"  # 13 characters, and 20 once converted

    def grown(self, limit: int) -> str:
        return "b" * (limit - len(self.NAME)) + self.NAME  # at the limit as pasted, 7 over once converted

    def test_text_that_conversion_took_past_its_limit_comes_back_whole_with_a_note(self):
        content = "a" * (1990 - 3 * len(self.NAME)) + self.NAME * 3
        made, notes = convert_message({"content": content})
        self.assertEqual(made["content"], "a" * 1951 + "{user(profile_name)}" * 3)
        self.assertEqual(notes, ["Message text: once converted this is 2,011 characters, and it can hold 2,000. Save refuses the message until it is 11 characters shorter."])
        with self.assertRaises(Invalid):
            messages.clean_payload(made, templates=True)
        made, notes = convert_message({"content": "c" * (2000 - len(self.NAME) - 1) + self.NAME})
        self.assertEqual((len(made["content"]), notes), (2006, ["Message text: once converted this is 2,006 characters, and it can hold 2,000. Save refuses the message until it is 6 characters shorter."]))
        made, notes = convert_message({"content": "c" * (2000 - len(self.NAME) - 6) + self.NAME})
        self.assertEqual((len(made["content"]), notes), (2001, ["Message text: once converted this is 2,001 characters, and it can hold 2,000. Save refuses the message until it is 1 character shorter."]))

    def test_each_part_that_grew_past_its_limit_is_named(self):
        data = {"embeds": [{"title": self.grown(256), "url": "https://example.com/{{User.ID}}", "color": 3553599, "footer": {"text": self.grown(2048), "icon_url": "https://example.com/i.png"},
                            "author": {"name": self.grown(256)}, "fields": [{"name": "kept", "value": "short"}, {"name": self.grown(256), "value": self.grown(1024), "inline": True}]}],
                "buttons": [{"url": "https://example.com", "label": "fine"}, {"url": "https://example.com/b", "label": self.grown(80)}]}
        made, notes = convert_message(data)
        grown = lambda limit: "b" * (limit - len(self.NAME)) + "{user(profile_name)}"
        self.assertEqual(made, {"content": "", "embeds": [{"title": grown(256), "url": "https://example.com/{user(id)}", "color": 3553599,
                                                           "footer": {"text": grown(2048), "icon_url": "https://example.com/i.png"}, "author": {"name": grown(256)},
                                                           "fields": [{"name": "kept", "value": "short", "inline": False}, {"name": grown(256), "value": grown(1024), "inline": True}]}],
                                "buttons": [{"url": "https://example.com", "label": "fine"}, {"url": "https://example.com/b", "label": grown(80)}]})
        self.assertEqual([note.split(":")[0] for note in notes], ["Embed 1 title", "Embed 1 footer", "Embed 1 author", "Embed 1 field 2 name", "Embed 1 field 2 value", "Button 2 label"])
        self.assertIn("Embed 1 field 2 value: once converted this is 1,031 characters, and it can hold 1,024.", notes[4])
        made, notes = convert_message({"embeds": [{"description": self.grown(4096)}]})
        self.assertEqual((len(made["embeds"][0]["description"]), notes), (4103, ["Embed 1 description: once converted this is 4,103 characters, and it can hold 4,096. Save refuses the message until it is 7 characters shorter."]))

    def test_embeds_that_grew_past_their_total_are_named_together(self):
        made, notes = convert_message({"embeds": [{"description": self.grown(3000)}, {"description": self.grown(3000)}]})
        self.assertEqual([len(embed["description"]) for embed in made["embeds"]], [3007, 3007])
        self.assertEqual(notes, ["Embeds: once converted they hold 6,014 characters together, and a message's embeds can hold 6,000. Save refuses the message until they are 14 characters shorter."])

    def test_what_is_wrong_besides_a_length_is_still_refused(self):
        with mock.patch.object(doublebrace, "_usable_link", return_value=True):
            with self.assertRaises(Invalid):
                convert_message({"content": self.grown(2000), "embeds": [{"title": "t", "url": "not a link"}]})
        made, notes = convert_message({"content": self.grown(1000)})
        self.assertEqual((len(made["content"]), notes), (1007, []), "inside every limit, nothing to say")
        made, notes = convert_message({"content": "d" * 1980 + self.NAME, "embeds": [{"description": "e" * 3000}, {"description": "f" * 3000}]})
        self.assertEqual(([len(made["content"])] + [len(embed["description"]) for embed in made["embeds"]], notes), ([2000, 3000, 3000], []), "at each limit exactly")


class MessageTest(Case):
    def test_every_part_of_a_message(self):
        data = {
            "content": "Hi {{User.Mention}}",
            "embeds": [{
                "title": "Welcome {{User.Name}}", "description": "You are the {{Ordinal(Guild.Members)}}", "url": "https://example.com/{{User.ID}}", "color": 3553599, "timestamp": "2026-11-11T12:00:00+00:00",
                "fields": [{"name": "Joined {{Guild.Name}}", "value": "{{User.JoinedAt}}", "inline": True}, {"name": "Count", "value": "{{Guild.MembersJoined}}"}],
                "image": {"url": "{{Guild.Banner}}"}, "thumbnail": {"url": "{{User.Avatar}}"},
                "footer": {"text": "{{Guild.Name}}", "icon_url": "{{Guild.Icon}}"}, "author": {"name": "{{User.Username}}", "url": "https://example.com/u", "icon_url": "{{User.Avatar}}"},
            }],
            "components": [{"type": 1, "components": [{"type": 2, "style": 5, "label": "Rules of {{Guild.Name}}", "url": "https://example.com/rules/{{Guild.ID}}", "emoji": {"name": "\N{SCROLL}"}}]}],
        }
        made, notes = convert_message(data)
        self.assertEqual(notes, [])
        self.assertEqual(made, {
            "content": "Hi {mention}",
            "embeds": [{
                "title": "Welcome {user(profile_name)}", "description": "You are the {ord(plain):{server(members)}}", "url": "https://example.com/{user(id)}", "color": 3553599, "timestamp": "2026-11-11T12:00:00+00:00",
                "footer": {"text": "{server}", "icon_url": "{server(icon)}"}, "author": {"name": "{user(name)}", "url": "https://example.com/u", "icon_url": "{user(profile_avatar)}"},
                "image": {"url": "{server(banner)}"}, "thumbnail": {"url": "{user(profile_avatar)}"},
                "fields": [{"name": "Joined {server}", "value": "{user(joined_ago)}", "inline": True}, {"name": "Count", "value": "{joined}", "inline": False}],
            }],
            "buttons": [{"url": "https://example.com/rules/{server(id)}", "label": "Rules of {server}", "emoji": "\N{SCROLL}"}],
        })
        self.assertEqual(json.loads(json.dumps(made)), convert_message(json.dumps(data))[0], "the same message as JSON text converts the same")
        shown = render_payload(made, self.scope())
        self.assertEqual(shown["content"], "Hi <@1234567890>")
        self.assertEqual(shown["embeds"][0]["url"], "https://example.com/1234567890")
        self.assertEqual(shown["embeds"][0]["fields"][1]["value"], "123456")
        self.assertNotIn("image", shown["embeds"][0], "a link that comes out empty is dropped when the message goes out")
        self.assertEqual(shown["buttons"], [{"url": "https://example.com/rules/1234567890", "label": "Rules of Test Server", "emoji": "\N{SCROLL}"}])

    def test_the_shapes_a_message_comes_in(self):
        wanted = {"content": "", "embeds": [{"title": "Hi {user(profile_name)}"}], "buttons": []}
        for data in ({"embeds": [{"title": "Hi {{User.Name}}"}]}, {"embed": {"title": "Hi {{User.Name}}"}}, [{"title": "Hi {{User.Name}}"}], {"title": "Hi {{User.Name}}"},
                     {"messages": [{"data": {"embeds": [{"title": "Hi {{User.Name}}"}]}}]}, '{"embeds": [{"title": "Hi {{User.Name}}"}]}', '[{"title": "Hi {{User.Name}}"}]'):
            self.assertEqual(convert_message(data)[0], wanted, data)
        self.assertEqual(convert_message({"content": "Hi {{User.Name}}", "embeds": None, "components": None})[0], {"content": "Hi {user(profile_name)}", "embeds": [], "buttons": []})
        self.assertEqual(convert_message({"buttons": [{"label": "Go {{Guild.Name}}", "url": "https://example.com", "emoji": "<:wave:123456789012345678>"}]})[0]["buttons"],
                         [{"url": "https://example.com", "label": "Go {server}", "emoji": "<:wave:123456789012345678>"}])
        self.assertEqual(convert_message({"components": [{"components": [{"url": "https://example.com", "emoji": {"id": "123456789012345678", "name": "wave", "animated": True}}]}]})[0]["buttons"],
                         [{"url": "https://example.com", "emoji": "<a:wave:123456789012345678>"}])

    def test_the_number_format_reaches_every_part(self):
        made, _ = convert_message({"content": "{{Ordinal(Guild.Members)}}", "embeds": [{"description": "{{FormatNumber(Guild.MembersJoined)}}"}]}, number_format="dots")
        self.assertEqual((made["content"], made["embeds"][0]["description"]), ("{ord(dots):{server(members)}}", "{num(dots):{joined}}"))

    def test_notes_say_which_part_they_are_about(self):
        _, notes = convert_message({"content": "{{Nope}}", "embeds": [{"title": "{{Nope}}", "description": "fine"}, {"description": "x", "fields": [{"name": "{{Never}}", "value": "{{Nope}}"}],
                                                                       "footer": {"text": "{{Guild.Members / 2}}"}}]})
        self.assertEqual(notes, [
            "Message text: `{{Nope}}` has no match in the template language, so it stays as written.",
            "Embed 1 title: `{{Nope}}` has no match in the template language, so it stays as written.",
            "Embed 2 footer: `{{Guild.Members / 2}}` is worked out by the math block, which writes this kind of result with a decimal point, so 8/2 shows as 4.0.",
            "Embed 2 field 1 name: `{{Never}}` has no match in the template language, so it stays as written.",
            "Embed 2 field 1 value: `{{Nope}}` has no match in the template language, so it stays as written.",
        ])

    def test_parts_that_can_not_go_into_a_greeting_are_named_not_dropped_quietly(self):
        made, notes = convert_message({
            "content": 5, "tts": True, "flags": 4096, "allowed_mentions": {"parse": ["everyone"]}, "attachments": [], "username": "",
            "embeds": [
                {"title": "", "url": "https://example.com", "color": "#zzzzzz", "timestamp": "yesterday", "footer": {"icon_url": "https://example.com/i.png"}, "author": {"url": "https://example.com"},
                 "image": {"url": "not a link"}, "thumbnail": {"url": 7}, "fields": [{"name": "only a name"}, {"value": "only a value"}, "nonsense", {"name": "kept", "value": "kept"}]},
                "nonsense", {"color": 5}, {},
            ],
            "components": [{"type": 1, "components": [{"type": 2, "style": 1, "custom_id": "press", "label": "Press"}, {"type": 2, "style": 5, "url": "https://example.com"},
                                                      {"type": 2, "style": 5, "url": "javascript:alert(1)", "label": "Bad"}]}, "nonsense"],
        })
        self.assertEqual(made, {"content": "", "embeds": [{"fields": [{"name": "kept", "value": "kept", "inline": False}]}], "buttons": []})
        self.assertEqual(notes, [
            "Message text: this isn't text, so it was left out.",
            "Embed 1 title link: a title link needs a title, so it was left out.",
            "Embed 1 color: `#zzzzzz` isn't a color, so it was left out.",
            "Embed 1 timestamp: `yesterday` isn't a date and time, so it was left out.",
            "Embed 1 footer icon: a footer icon needs footer text, so it was left out.",
            "Embed 1 author: an author link or icon needs an author name, so it was left out.",
            "Embed 1 image: `not a link` isn't a link, so it was left out.",
            "Embed 1 thumbnail: this isn't text, so it was left out.",
            "Embed 1 field 1: a field needs a name and a value, so it was left out.",
            "Embed 1 field 2: a field needs a name and a value, so it was left out.",
            "Embed 1 field 3: a field needs a name and a value, so it was left out.",
            "Embed 2: this isn't an embed, so it was left out.",
            "Embed 3: it has nothing to show, so it was left out.",
            "Buttons: 1 of them is not a link button. A greeting carries link buttons only, so it was left out.",
            "Button 1: a button needs a link and a label or an emoji, so it was left out.",
            "Button 2: `javascript:alert(1)` isn't a link, so it was left out.",
            "Message: a greeting has no place for `allowed_mentions`, `flags`, `tts`, so that was left out.",
        ])

    def test_too_many_of_something_keeps_the_first_ones_and_says_so(self):
        many = {"embeds": [{"description": str(number), "fields": [{"name": "n", "value": "v"}] * 30} for number in range(12)],
                "buttons": [{"label": str(number), "url": "https://example.com"} for number in range(30)]}
        made, notes = convert_message(many)
        self.assertEqual((len(made["embeds"]), len(made["embeds"][0]["fields"]), len(made["buttons"])), (10, 25, 25))
        self.assertIn("Embeds: a message holds 10, so the last 2 were left out.", notes)
        self.assertIn("Embed 1 fields: an embed holds 25, so the last 5 were left out.", notes)
        self.assertIn("Buttons: a message holds 25, so the last 5 were left out.", notes)

    def test_what_is_not_a_message_is_refused_with_a_sentence(self):
        for data, said in [("not json {", "isn't JSON"), ("5", "isn't a message or an embed"), ('"text"', "isn't a message or an embed"), (None, "isn't a message or an embed"),
                           ("[" * 100000, "isn't JSON"), ({"content": "x" * 2001}, "2000"), ({"embeds": [{"title": "x" * 300}]}, "Title can be at most 256"),
                           ({"embeds": [{"description": "x" * 4000}, {"description": "y" * 4000}]}, "6000")]:
            with self.assertRaises(Invalid) as refused:
                convert_message(data)
            self.assertIn(said, str(refused.exception), data if not isinstance(data, str) else data[:20])
        with self.assertRaises(Invalid):
            convert_message({"content": "x"}, number_format="roman")
        with self.assertRaises(Invalid):
            convert_text("x", number_format="roman")
        with self.assertRaises(Invalid):
            convert_text(5)


class LimitsTest(Case):
    """Pasted text can be anything. It converts in bounded time or is refused with a sentence, and nothing else comes out of it."""

    def test_text_past_the_limits_is_refused(self):
        with self.assertRaises(Invalid) as refused:
            convert_text("x" * (doublebrace.MAX_TEXT + 1))
        self.assertIn("converts up to 100000", str(refused.exception))
        with self.assertRaises(Invalid) as refused:
            convert_text("{{User.Name}}" * (doublebrace.MAX_TAGS + 1))
        self.assertIn("more than 2000 tags", str(refused.exception))

    def test_deep_sections_and_deep_parentheses_do_not_overflow(self):
        deep = "{{#User.Bot}}" * 300 + "x" + "{{/User.Bot}}" * 300
        made, notes = convert_text(deep)
        self.assertTrue(any("sections deep" in note for note in notes))
        self.assertEqual(render_text(made, self.scope()).count("x"), 0, "the outer sections still decide: a person sees nothing of a bot's text")
        made, notes = convert_text("{{" + "(" * 60 + "1" + ")" * 60 + "}}")
        self.assertIn("nests too deeply", notes[0])
        made, notes = convert_text("{{(" + "!" * 100 + "User.Bot)}}")
        self.assertIn("nests too deeply", notes[0])
        for long in ("{{" + "(" * 500 + "1" + ")" * 500 + "}}", "{{" + " + ".join(["1"] * 500) + "}}", "{{" + "-" * 5000 + "1}}"):
            made, notes = convert_text(long)
            self.assertIn("is too long to read", notes[0])
            self.assertEqual(render_text(made, self.scope()), long)
        self.assertEqual(self.shown("{{" + " + ".join(["1"] * 90) + "}}"), "90")
        self.assertEqual(self.shown("{{" + "(" * 15 + "Guild.Members" + ")" * 15 + "}}"), "1234")

    def test_the_engine_is_asked_about_a_bounded_number_of_short_pairs(self):
        made, notes = convert_text("{" + "x" * 3000 + "}")
        self.assertEqual(made, OPEN_BRACE + "x" * 3000 + CLOSE_BRACE, "a long pair is written out without asking")
        self.assertEqual(notes, [doublebrace.BRACE_NOTE])
        made, _ = convert_text("{a}" * (doublebrace.MAX_PROBES + 5))
        self.assertEqual(made.count(OPEN_BRACE), 5, "past the number it asks about, a pair is written out")
        self.assertEqual(render_text(made, self.scope()), "{a}" * (doublebrace.MAX_PROBES + 5))

    def asked(self):
        """The engine's answers, counted: how many brace pairs a conversion asked it about."""
        real = doublebrace._stays_plain
        calls = []

        def counted(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        return mock.patch.object(doublebrace, "_stays_plain", counted), calls

    def test_a_whole_message_is_one_call_with_one_budget(self):
        pairs = "{" * 400 + "x" * 224 + "}" * 400  # a field's 1,024 characters, holding 400 pairs to ask about
        message = {"content": "{" * 400 + "x" * 1198 + "}" * 400, "embeds": [{"fields": [{"name": "n", "value": pairs} for _ in range(5)]}]}
        counting, calls = self.asked()
        began = time.thread_time()
        with counting:
            try:
                convert_message(message)
            except Invalid:
                pass  # written out as braces, a part can come out too long to store. What's measured here is the work.
        self.assertLessEqual(len(calls), doublebrace.MAX_PROBES, "six parts share the one count, where each used to get its own")
        self.assertLess(time.thread_time() - began, 2.5, "about a second of processor time, however many parts there are")

    def test_a_call_that_has_used_its_time_writes_the_rest_out_without_asking(self):
        counting, calls = self.asked()
        with counting, mock.patch.object(doublebrace, "MAX_SECONDS", 0.0):
            made, notes = convert_text("{user} and {mention} and {nobody}")
        self.assertEqual(calls, [])
        self.assertEqual(made.count(OPEN_BRACE), 3)
        self.assertEqual(render_text(made, self.scope()), "{user} and {mention} and {nobody}", "written out, it shows the same")
        self.assertEqual(notes, [doublebrace.BRACE_NOTE])
        counting, calls = self.asked()
        with counting:
            self.assertEqual(convert_text("{nobody} and {user}")[0], "{nobody} and " + OPEN_BRACE + "user" + CLOSE_BRACE, "with time in hand it asks, and leaves alone what stays plain")
        self.assertEqual(len(calls), 2)

    def test_one_slow_answer_can_not_run_past_the_call_s_time(self):
        seen = []
        real = doublebrace.Interpreter.render

        def render(run, source):
            seen.append(run._deadline - time.thread_time())
            return real(run, source)

        with mock.patch.object(doublebrace.Interpreter, "render", render), mock.patch.object(doublebrace, "MAX_SECONDS", 0.05):
            convert_text("{nobody}")
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], 0.05 + 1e-9, "the engine's own quarter second is cut to what the call has left")

    def test_a_part_too_long_as_it_stands_is_refused_before_any_work(self):
        value = "{" * 400 + "x" * 1198 + "}" * 400  # 1,998 characters in a field that holds 1,024
        slow = json.dumps({"embeds": [{"fields": [{"name": "n", "value": value} for _ in range(24)]}]})
        self.assertLess(len(slow), 50_000, "inside what the Import box takes")
        counting, calls = self.asked()
        began = time.thread_time()
        with counting, self.assertRaises(Invalid) as refused:
            convert_message(slow)
        self.assertEqual(str(refused.exception), "Embed 1: Field 1 value can be at most 1024 characters. This is 1998.")
        self.assertEqual(calls, [], "the lengths are checked first")
        self.assertLess(time.thread_time() - began, 1.0)
        for message, sentence in [
            ({"content": "x" * 2001}, "The message text can be at most 2000 characters. This is 2001."),
            ({"embeds": [{"title": "t" * 257}]}, "Embed 1: Title can be at most 256 characters. This is 257."),
            ({"embeds": [{"title": "t"}, {"description": "d" * 4097}]}, "Embed 2: Description can be at most 4096 characters. This is 4097."),
            ({"embeds": [{"footer": {"text": "f" * 2049}}]}, "Embed 1: Footer can be at most 2048 characters. This is 2049."),
            ({"embeds": [{"author": {"name": "a" * 257}}]}, "Embed 1: Author name can be at most 256 characters. This is 257."),
            ({"embeds": [{"fields": [{"name": "n" * 257, "value": "v"}]}]}, "Embed 1: Field 1 name can be at most 256 characters. This is 257."),
            ({"embeds": [{"description": "d" * 4000}, {"description": "e" * 2001}]}, "All embeds together can hold at most 6000 characters. These hold 6001."),
            ({"buttons": [{"url": "https://example.com", "label": "b" * 81}]}, "Button 1 label can be at most 80 characters. This is 81."),
            ({"embeds": [{"description": "d" * 4000}, {"description": "e" * 2001}], "buttons": [{"url": "https://example.com", "label": "b" * 81}]},
             "All embeds together can hold at most 6000 characters. These hold 6001."),
        ]:
            counting, calls = self.asked()
            with counting, self.assertRaises(Invalid, msg=sentence) as refused:
                convert_message({**message, "content": message.get("content", "{a} {b}")})
            self.assertEqual((str(refused.exception), calls), (sentence, []))

    def test_a_link_too_long_to_be_one_is_left_out_without_converting(self):
        counting, calls = self.asked()
        long_link = "https://example.com/" + "{a}" * 700
        with counting:
            made, notes = convert_message({"embeds": [{"title": "t", "url": long_link}]})
        self.assertEqual((calls, made["embeds"]), ([], [{"title": "t"}]))
        self.assertEqual(notes, [f"Embed 1 title link: `{long_link[:77]}...` isn't a link, so it was left out."])

    def test_a_pair_the_engine_chokes_on_is_written_out(self):
        with mock.patch.object(doublebrace.Interpreter, "render", side_effect=RuntimeError("the engine broke")):
            self.assertEqual(convert_text("{a} b"), (OPEN_BRACE + "a" + CLOSE_BRACE + " b", [doublebrace.BRACE_NOTE]))

    def test_many_braces_convert_quickly(self):
        for hostile in ("{" * 40000 + "}" * 40000, "{user}" * 15000, "{{^User.Bot}}" + "{}" * 20000 + "{{/User.Bot}}", "{" * 50000, "}" * 50000 + "{" * 50000, "{{" * 49999):
            began = time.perf_counter()
            made, _ = convert_text(hostile)
            self.assertLess(time.perf_counter() - began, 20, hostile[:20])
            self.assertIsInstance(made, str)

    def test_random_text_never_raises_anything_but_a_refusal(self):
        generator = random.Random(20261111)
        pieces = ["{{", "}}", "{", "}", "#", "^", "/", "!", "&", ">", "=", "User.Bot", "User.Name", "Guild.Members", "Invite", " ", "\n", "(", ")", "|", ":", "\"", "'", "?", "+", "-", "*", "==", "&&", "||",
                  "Upper(", "Ordinal(", "FormatTime(", ",", "x", "1", "<%", "%>", "\\", "&amp;", "{user}", "{if(1==1):a|b}"]
        for _ in range(1500):
            source = "".join(generator.choice(pieces) for _ in range(generator.randint(1, 40)))
            try:
                made, notes = convert_text(source)
            except Invalid:
                continue
            self.assertIsInstance(made, str, source)
            self.assertTrue(all(isinstance(note, str) for note in notes))
            render_text(made, self.scope())

    def test_plain_text_always_comes_through_as_it_was(self):
        """Whatever single braces a text holds, with no tags in it the converted template shows that text again."""
        generator = random.Random(7)
        pieces = ["{", "}", "{user}", "{server(members)}", "{if(1==1):a|b}", "{upper:x}", "{=(a):b}", "{a}", "{1}", "{unix}", "{joined}", "{m:1+1}", " ", "a", ":", "(", ")", "|", "{invite(code)}", "{embed(title):t}"]
        tried = 0
        for _ in range(900):
            source = "".join(generator.choice(pieces) for _ in range(generator.randint(1, 12))).strip()
            if "{{" in source:
                continue  # two braces together open a tag, so that isn't plain text to either syntax
            tried += 1
            made, notes = convert_text(source)
            self.assertEqual(render_text(made, self.scope()), source, (source, made))
            self.assertEqual(notes, [doublebrace.BRACE_NOTE] if OPEN_BRACE in made else [])
        self.assertGreater(tried, 300)

    def test_a_section_s_text_always_comes_through_as_it_was(self):
        generator = random.Random(11)
        pieces = ["{", "}", "{user}", "|", "{if(1==1):a|b}", "(", ")", ":", "a", " ", "{x}", "==", "{mention}"]
        tried = 0
        for _ in range(900):
            inner = "".join(generator.choice(pieces) for _ in range(generator.randint(1, 10)))
            if "{{" in inner or inner.endswith("{"):
                continue  # those open a tag of their own
            tried += 1
            made, _ = convert_text("[{{^User.Bot}}" + inner + "{{/User.Bot}}]")
            self.assertEqual(render_text(made, self.scope()), "[" + inner + "]", (inner, made))
            made, _ = convert_text("[{{#User.Bot}}" + inner + "{{/User.Bot}}]")
            self.assertEqual(render_text(made, self.scope()), "[]", (inner, made))
        self.assertGreater(tried, 300)
