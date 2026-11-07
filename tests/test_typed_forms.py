"""What a moderator types lands on the action it names, and a line the bot can't use is refused in a plain sentence with nothing changed."""
from __future__ import annotations

from unittest import mock

import discord
from discord.ext import commands

from captainguild.core.ops import DiscordOps
from captainguild.core.perms import MODULE as COMMANDS_MODULE
from captainguild.core.shortcuts import BARE_FORMS, FIRST_WORD_FORMS, SHORTCUTS, TAG_FIRST_SHORTCUTS, expand_line, named_command
from captainguild.modules import load_all
from captainguild.modules.automod import parse_rate
from captainguild.modules.roles import RoleNames, find_roles
from tests.gateway import FakeHTTPResponse, GatewayTest, new_id

# Words of the library's that must never reach a person.
LIBRARY_TEXT = ("Could not convert", "Converting to", "is a required argument", "recognised", "Expected closing", "Too many arguments", "value must be", "You are on cooldown", "Unexpected quote", "not found.")


class Typed(GatewayTest):
    modules = load_all()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.ops = DiscordOps(self.bot)
        self.net.keep_history = True
        self.general = self.text["id"]
        self.logs_channel = self.log_channel["id"]
        self.member_mention = f"<@{self.member_id}>"
        # Over the network, the library's lookups for a name or an ID it doesn't have come back empty.
        for name in ("query_member_by_id", "query_member_named"):
            patcher = mock.patch.object(commands.MemberConverter, name, mock.AsyncMock(return_value=None))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.role_mention = f"<@&{self.role_member['id']}>"

    async def typed(self, line: str, user_id: int | None = None) -> str:
        """What the bot said to this line."""
        self.net.posted.clear()
        self.dispatch_message(user_id=user_id or self.owner_id, content=line)
        await self.settle(1.5)
        self.assert_no_errors(line)
        said = []
        for message in self.net.posted:
            said.append(message.get("content") or "")
            for embed in message.get("embeds") or []:
                said += [str(embed.get("title") or ""), str(embed.get("description") or "")]
                said += [f"{field['name']} {field['value']}" for field in embed.get("fields") or []]
        return "\n".join(said)

    def cfg(self, module: str) -> dict:
        return self.bot.settings.get(self.guild_id, module)


class ReadTheLineTest(Typed):
    """The long spelling each documented short form comes to."""

    FORMS = [
        ("slowmode 5 25", "automod slowmode set 5 25"), ("sm 5 25", "automod slowmode set 5 25"), ("slowmode p delete, tempmute 20m", "automod slowmode p delete, tempmute 20m"),
        ("attachmentspam 3 5", "automod attachmentspam set 3 5"), ("attachmentspam p mute, defer", "automod attachmentspam p mute, defer"),
        ("mentionspam 25 5", "automod mentionspam set 25 5"), ("mentionspam p tempban 24h", "automod mentionspam p tempban 24h"),
        ("linkspam 1 1", "automod linkspam rate 1 1"), ("linkspam bl a.com b.com", "automod linkspam blacklist add a.com b.com"), ("linkspam wl a.com", "automod linkspam whitelist add a.com"),
        ("linkspam unbl a.com", "automod linkspam blacklist remove a.com"), ("linkspam unwl a.com", "automod linkspam whitelist remove a.com"),
        ("linkspam clearbl", "automod linkspam clear blacklist"), ("linkspam clearwl", "automod linkspam clear whitelist"),
        ("censor boat", "automod censor words add boat"), ("censor add boat", "automod censor words add boat"), ("censor remove boat", "automod censor words remove boat"), ("censor list", "automod censor display"),
        ("am censor add boat", "automod censor words add boat"), ("capsp delete, warn", "automod caps punishment delete, warn"), ("capspunishment ban", "automod caps punishment ban"),
        ("honeypot add #x", "automod honeypot add #x"), ("am wl mods #x", "automod whitelist add mods #x"),
        ("af", "feeds autofeeds"), ("autofeed", "feeds autofeeds"), ("ae hi", "fun aesthetics hi"), ("bf hi", "fun boldfancy hi"), ("ds hi", "fun double hi"), ("ff hi", "fun fancy hi"), ("sc hi", "fun smallcaps hi"), ("ud hi", "fun urbandictionary hi"),
        ("coin", "fun coinflip"), ("flip", "fun coinflip"), ("pick a, b", "fun pick a, b"), ("choice a, b", "fun pick a, b"), ("select a, b", "fun pick a, b"), ("steal x :y:", "fun addemoji x :y:"),
        ("log timeout", "log config timeout"), ("i @x", "members info @x"), ("newmembers 3", "members newusers 3"), ("oldmembers 3", "members oldusers 3"),
        ("rm birthday 1d", "reminder birthday 1d"), ("timer birthday 1d", "reminder birthday 1d"), ("ar", "triggers"), ("autoresponse", "triggers"), ("commands", "tag list"),
        ("stickylist", "sticky"), ("stickyremove", "sticky remove"), ("stickytemplate Stick", "sticky fromtemplate Stick"), ("sticky No Spam", "sticky add No Spam"),
        ("yt link", "youtube add link"), ("youtube link", "youtube add link"), ("twitch tansmh #x hi", "twitch add tansmh #x hi"), ("twitch fmt a b", "twitch format a b"),
        ("giveaway 1h 2 Nitro", "giveaway create 1h 2 Nitro"), ("starboard #stars", "starboard setup #stars"), ("star #stars", "starboard setup #stars"), ("star emoji x", "starboard emoji set x"),
        ("lvl reset @x", "level reset member @x"), ("level reset all", "level reset all"), ("games alerts enable #x", "games alerts free-on #x"), ("games alerts disable", "games alerts free-off"),
        ("set welcome #x", "greetings set_welcome #x"), ("set farewell #x", "greetings set_farewell #x"), ("set twitch #x", "twitch default_channel #x"), ("cleanconfig", "config cleanconfig"),
        ("modrole clear", "config clearmodrole"), ("invite", "misc invite"), ("triggers ignore @x #y", "triggers ignore add @x #y"), ("level blacklist #x", "level blacklist add #x"),
    ]

    SAME_COMMAND_NAMES = [
        ("slowmode punish x", "automod slowmode punishment"), ("linkspam p x", "automod linkspam punishment"), ("invitespam punish x", "automod invitespam punishment"),
        ("honeypot + #x", "automod honeypot add"), ("honeypot - #x", "automod honeypot remove"), ("feeds del got", "feeds delete"), ("feeds remove got", "feeds delete"),
        ("af remove 1", "feeds autofeeds delete"), ("af del 1", "feeds autofeeds delete"), ("log ip !", "log prefix"), ("log up !", "log remove_prefix"), ("log custom 1", "log import"),
        ("star top", "starboard stats"), ("star source", "starboard jump"), ("suggestion anon", "suggestion anonymous"), ("triggers add a b", "triggers create"), ("triggers s a b", "triggers strict"),
        ("triggers e a b", "triggers exact"), ("triggers sw a b", "triggers startswith"), ("triggers ew a b", "triggers endswith"), ("triggers cs a b", "triggers channel"), ("triggers del 1", "triggers remove"),
        ("triggers delete 1", "triggers remove"), ("triggers - 1", "triggers remove"), ("triggers role_bl a 1", "triggers role_blacklist"), ("triggers role_wl a 1", "triggers role_whitelist"),
        ("hl + a", "highlight add"), ("hl m a", "highlight matches"), ("hl match a", "highlight matches"), ("hl - a", "highlight remove"), ("hl del a", "highlight remove"),
        ("rm - 42", "reminder cancel"), ("reminder remove 42", "reminder cancel"), ("reminder del 42", "reminder cancel"), ("yt sub a", "youtube add"), ("yt unsub a", "youtube remove"),
        ("purge pings 25", "purge mentions"), ("purge humans 13", "purge human"), ("lvl msgxp 15", "level messagexp"), ("lvl messagexp", "level messagexp"), ("am unwl a", "automod unwhitelist"), ("am unwhitelist a", "automod unwhitelist"),
        ("autorole readd", "autorole reassign"), ("autorole bl a", "autorole blacklist"), ("tr", "timedrole"), ("timedrole + 2h a", "timedrole add"), ("timedrole - a", "timedrole remove"),
    ]

    async def test_each_short_form_comes_to_its_long_spelling(self):
        for line, long_form in self.FORMS:
            with self.subTest(line=line):
                self.assertEqual(expand_line(self.bot, line), long_form)

    async def test_each_alias_reaches_its_command(self):
        for line, name in self.SAME_COMMAND_NAMES:
            with self.subTest(line=line):
                view_words = expand_line(self.bot, line).split()
                command = self.bot.all_commands[view_words[0]]
                for word in view_words[1:]:
                    child = getattr(command, "all_commands", {}).get(word)
                    if child is None:
                        break
                    command = child
                self.assertEqual(command.qualified_name, name)

    async def test_a_line_with_nothing_to_read_comes_back_the_same(self):
        for line in ("ban @x spam", "log channel #x", "tag + a b", "nothing here", "purge contains a"):
            with self.subTest(line=line):
                self.assertEqual(expand_line(self.bot, line), line)

    async def test_the_rest_of_the_line_stays_as_typed(self):
        self.assertEqual(expand_line(self.bot, 'sm  5\n25'), "automod slowmode set  5\n25")
        self.assertEqual(expand_line(self.bot, 'censor "two  words"'), 'automod censor words add "two  words"')

    async def test_the_word_a_group_goes_by_on_slash_is_its_own_function(self):
        for line, long_form in (("hl show", "highlight"), ("star config", "starboard"), ("log show", "log"), ("feeds list", "feeds"), ("twitch list", "twitch"), ("sticky list", "sticky"), ("tag get x", "tag x")):
            with self.subTest(line=line):
                self.assertEqual(expand_line(self.bot, line), long_form)

    async def test_a_word_after_the_slash_name_doesnt_become_a_bare_form(self):
        for line in ("twitch list extra", "sticky list foo", "tag get list"):
            with self.subTest(line=line):
                self.assertEqual(expand_line(self.bot, line), line)
        said = await self.typed("!twitch list extra")
        self.assertIn("no option called `list`", said)

    async def test_a_capital_letter_finds_the_subcommand(self):
        self.assertEqual(expand_line(self.bot, "Log Channel #x"), "log channel #x")

    async def test_every_table_entry_leads_to_a_real_command(self):
        for table in (SHORTCUTS, TAG_FIRST_SHORTCUTS):
            for name, target in table.items():
                with self.subTest(name=name):
                    words = target.split()
                    self.assertIsNotNone(self.bot.get_command(" ".join(words)) or self.bot.get_command(words[0]), target)
        for group, target in BARE_FORMS.items():
            with self.subTest(group=group):
                self.assertTrue(hasattr(self.bot.get_command(group), "all_commands"), group)
                self.assertIsNotNone(self.bot.get_command(target) or self.bot.get_command(" ".join(target.split()[:-1])), target)
        for name, (_, target) in FIRST_WORD_FORMS.items():
            with self.subTest(name=name):
                self.assertIsNotNone(self.bot.get_command(name), name)
                self.assertIsNotNone(self.bot.get_command(" ".join(target.split()[:2])), target)


class StrictParsingTest(Typed):
    async def test_a_mistyped_channel_leaves_the_setting_alone(self):
        await self.bot.settings.save(self.guild_id, "logging", {"default_channel": int(self.logs_channel)})
        said = await self.typed("!log channel gneral")
        self.assertIn("I can't find a channel called `gneral`", said)
        self.assertIn("nothing changed", said)
        self.assertIn("Usage: `!log channel [channel]`", said)
        self.assertEqual(self.cfg("logging")["default_channel"], int(self.logs_channel))

    async def test_an_empty_channel_still_clears_it(self):
        await self.bot.settings.save(self.guild_id, "logging", {"default_channel": int(self.logs_channel)})
        await self.typed("!log channel")
        self.assertIsNone(self.cfg("logging")["default_channel"])

    async def test_every_channel_setting_keeps_its_value_on_a_typo(self):
        cases = [("!log message_channel gneral", "logging", "message_channel"), ("!birthday channel gneral", "greetings", "birthday_channel"), ("!level channel gneral", "levels", "destination")]
        for line, module, field in cases:
            with self.subTest(line=line):
                await self.bot.settings.save(self.guild_id, module, {field: int(self.logs_channel)})
                said = await self.typed(line)
                self.assertIn("gneral", said)
                self.assertEqual(self.cfg(module)[field], int(self.logs_channel))

    async def test_a_name_that_matches_no_member_says_so(self):
        for line in ("!members info zzz", "!birthday zzz", "!level member zzz", "!starboard stats zzz", "!warns zzz"):
            with self.subTest(line=line):
                said = await self.typed(line)
                self.assertIn("zzz", said)
                self.assertNotIn(str(self.owner_id), said)

    async def test_a_name_that_matches_no_role_says_so(self):
        said = await self.typed("!muterole zzz")
        self.assertIn("I can't find a role called `zzz`", said)

    async def test_a_group_refuses_a_word_with_no_use_and_lists_what_it_takes(self):
        said = await self.typed("!autorole nonsense")
        self.assertIn("`autorole` has no option called `nonsense`", said)
        self.assertIn("`add`", said)

    async def test_a_command_that_takes_nothing_refuses_more(self):
        said = await self.typed("!mutes these words")
        self.assertIn("I don't know what to do with `these words`", said)

    async def test_the_old_silent_lines_no_longer_print_a_listing(self):
        for line in ("!triggers nonsense here", "!autorole nonsense", "!suggestion nonsense", "!highlight nonsense"):
            with self.subTest(line=line):
                said = await self.typed(line)
                self.assertRegex(said, "no option|nothing changed")

    async def test_a_command_that_reads_its_own_leftover_words_answers_its_own_way(self):
        self.assertEqual((await self.typed("!misc permissions zzz")).strip()[-len("I can't find that channel."):], "I can't find that channel.")
        self.assertIn("I can't find that channel.", await self.typed("!botpermissions zzz"))
        self.assertIn("I can't find a channel called `zzz`", await self.typed("!autopurge remove zzz"))
        self.assertIn("I can't find a channel called `zzz`", await self.typed("!unlockdown zzz"))

    async def test_level_blacklist_still_answers_about_the_name(self):
        said = await self.typed("!level blacklist zzz")
        self.assertIn("zzz", said)
        self.assertEqual(self.cfg("levels")["blacklist_channels"], [])

    async def test_a_new_starboard_channel_needs_new_in_front(self):
        said = await self.typed("!starboard setup gneral")
        self.assertIn("I can't find a channel called `gneral`", said)
        self.assertNotIn("POST /guilds/{guild_id}/channels", [f"{a} {b}" for a, b, *_ in self.net.calls])
        self.assertIsNone(self.cfg("starboard")["channel_id"])
        await self.typed("!starboard setup new:hall")
        self.assertIn("POST /guilds/{guild_id}/channels", [f"{a} {b}" for a, b, *_ in self.net.calls])

    async def test_a_new_suggestion_channel_needs_new_in_front(self):
        said = await self.typed("!suggestion channel gneral")
        self.assertIn("I can't find a channel called `gneral`", said)
        self.assertIsNone(self.cfg("suggestions")["channel_id"])

    async def test_a_quote_that_never_closes_gets_a_sentence(self):
        said = await self.typed('!triggers create "open ended')
        self.assertIn("A quotation mark in that line doesn't close", said)


class PlainErrorsTest(Typed):
    async def test_library_words_never_reach_a_person(self):
        lines = ["!purge bot abc", "!purge user", "!starboard emoji", "!avatar nobody here", "!report", "!level reward", "!ban", "!kick zzz", "!role add", "!role info zzz", "!autorole add zzz", "!temprole add", "!reason abc def",
                 "!tag modonly maybe", "!leaderboard 0", "!leaderboard 999999999", "!level limit 99999", "!giveaway create 1h 99 Nitro", "!cute elephant", "!autopurge set #x 1h", "!feeds move a zzz", "!triggers role_blacklist zzz 1", "!suggestion approve abc", "!embed source"]
        for line in lines:
            with self.subTest(line=line):
                said = await self.typed(line)
                self.assertTrue(said, line)
                for words in LIBRARY_TEXT:
                    self.assertNotIn(words, said)

    async def test_a_missing_word_is_named_with_the_usage(self):
        said = await self.typed("!purge all")
        self.assertIn("That needs a count.", said)
        self.assertIn("Usage: `!purge all <count>`", said)

    async def test_a_wrong_choice_lists_the_choices(self):
        said = await self.typed("!cute elephant")
        self.assertIn("`elephant` isn't one of these: `cat`, `dog`", said)

    async def test_a_word_where_a_number_goes(self):
        said = await self.typed("!reason abc def")
        self.assertIn("`abc` isn't a whole number.", said)

    async def test_a_cooldown_says_how_long(self):
        await self.typed(f"!level {self.member_mention}", user_id=self.member_id)
        said = await self.typed(f"!level {self.member_mention}", user_id=self.member_id)
        self.assertRegex(said, r"Wait \d+ seconds and try again\.")

    async def test_a_converters_own_sentence_comes_through(self):
        said = await self.typed("!giveaway create soon 1 Nitro")
        self.assertIn("isn't a length of time", said)


class AutomodFormsTest(Typed):
    async def test_the_short_forms_change_the_rule(self):
        await self.typed("!slowmode 5 25")
        self.assertEqual(list(self.cfg("automod")["spam"]["rate"]), [5, 25])
        await self.typed("!attachmentspam 3 5")
        self.assertEqual(list(self.cfg("automod")["attachments"]["rate"]), [3, 5])
        await self.typed("!mentionspam 25 5")
        self.assertEqual(list(self.cfg("automod")["mentions"]["rate"]), [25, 5])
        await self.typed("!linkspam 2 3")
        self.assertEqual(list(self.cfg("automod")["links"]["rate"]), [2, 3])

    async def test_punishment_short_names(self):
        before = dict(self.cfg("automod")["spam"]["punishment"])
        said = await self.typed("!slowmode p ban")
        self.assertIn("now does", said)
        self.assertNotEqual(self.cfg("automod")["spam"]["punishment"], before)

    async def test_linkspam_lists(self):
        await self.typed("!linkspam bl reddit.com twitter.com")
        self.assertEqual(self.cfg("automod")["links"]["blacklist"], ["reddit.com", "twitter.com"])
        await self.typed("!linkspam unbl twitter.com")
        self.assertEqual(self.cfg("automod")["links"]["blacklist"], ["reddit.com"])
        await self.typed("!linkspam wl a.com")
        self.assertEqual(self.cfg("automod")["links"]["whitelist"], ["a.com"])
        await self.typed("!linkspam unwl a.com")
        self.assertEqual(self.cfg("automod")["links"]["whitelist"], [])
        await self.typed("!linkspam clearbl")
        self.assertEqual(self.cfg("automod")["links"]["blacklist"], [])

    async def test_censor_forms(self):
        await self.typed("!censor boat")
        self.assertEqual([entry["text"] for entry in self.cfg("automod")["words"]["entries"]], ["boat"])
        await self.typed("!censor add ship")
        self.assertEqual([entry["text"] for entry in self.cfg("automod")["words"]["entries"]], ["boat", "ship"])
        await self.typed("!censor remove boat")
        self.assertEqual([entry["text"] for entry in self.cfg("automod")["words"]["entries"]], ["ship"])
        with mock.patch("captainguild.modules.automod.send_private", mock.AsyncMock()) as sent:
            await self.typed("!censor list")
        self.assertIn("ship", sent.await_args.args[1])

    async def test_one_number_on_a_rule_means_that_many_in_a_second_except_slowmode(self):
        self.assertEqual(list(parse_rate("25", "count")), [25, 1])
        self.assertEqual(list(parse_rate("25")), [1, 25])
        await self.typed("!automod mentionspam set 25")
        self.assertEqual(list(self.cfg("automod")["mentions"]["rate"]), [25, 1])
        await self.typed("!attachmentspam 3")
        self.assertEqual(list(self.cfg("automod")["attachments"]["rate"]), [3, 1])
        await self.typed("!linkspam 4")
        self.assertEqual(list(self.cfg("automod")["links"]["rate"]), [4, 1])
        await self.typed("!slowmode 5")
        self.assertEqual(list(self.cfg("automod")["spam"]["rate"]), [1, 5])

    async def test_the_whitelist_takes_roles_and_channels_together(self):
        said = await self.typed(f"!am wl {self.role_mention} <#{self.general}> zzz")
        self.assertIn("zzz", said)
        self.assertEqual(self.cfg("automod")["allowed_roles"], [int(self.role_member["id"])])
        self.assertEqual(self.cfg("automod")["allowed_channels"], [int(self.general)])
        await self.typed(f"!am unwl {self.role_mention} <#{self.general}>")
        self.assertEqual(self.cfg("automod")["allowed_roles"], [])
        self.assertEqual(self.cfg("automod")["allowed_channels"], [])

    async def test_a_whitelist_of_nothing_real_changes_nothing(self):
        said = await self.typed("!am wl zzz")
        self.assertIn("I couldn't find: zzz", said)
        self.assertEqual(self.cfg("automod")["allowed_roles"], [])

    async def test_the_subcommands_say_which_names_they_couldnt_find(self):
        said = await self.typed(f"!automod whitelist channels add <#{self.general}> zzz")
        self.assertIn("I couldn't find: zzz", said)
        said = await self.typed(f"!automod whitelist roles add {self.role_mention} zzz")
        self.assertIn("I couldn't find: zzz", said)
        said = await self.typed("!automod unmedia zzz")
        self.assertIn("I couldn't find: zzz", said)

    async def test_honeypot_from_the_top(self):
        await self.typed(f"!honeypot add <#{self.general}>")
        self.assertEqual(self.cfg("automod")["honeypot"]["channels"], [int(self.general)])
        await self.typed(f"!honeypot - <#{self.general}>")
        self.assertEqual(self.cfg("automod")["honeypot"]["channels"], [])


class EmbedFormsTest(Typed):
    JSON = '{"title":"x"}'

    def embeds_in(self, channel_id: str) -> list:
        return [m for m in self.net.posted if m["channel_id"] == channel_id and m.get("embeds")]

    async def test_a_channel_before_or_after_the_json(self):
        for line in (f"!cembed <#{self.logs_channel}> {self.JSON}", f"!embed custom <#{self.logs_channel}> {self.JSON}", f"!embed custom {self.JSON} <#{self.logs_channel}>", f"!customembed {self.logs_channel} {self.JSON}"):
            with self.subTest(line=line):
                await self.typed(line)
                self.assertEqual(len(self.embeds_in(self.logs_channel)), 1)

    async def test_no_channel_posts_here(self):
        await self.typed(f"!cembed {self.JSON}")
        self.assertEqual(len(self.embeds_in(self.general)), 1)

    async def test_a_channel_with_no_json_says_so(self):
        said = await self.typed(f"!cembed <#{self.logs_channel}>")
        self.assertIn("JSON", said)
        self.assertEqual(self.embeds_in(self.logs_channel), [])

    async def test_editing_with_the_channel_before_the_json(self):
        await self.typed(f"!cembed <#{self.logs_channel}> {self.JSON}")
        posted = self.embeds_in(self.logs_channel)[0]
        for line in (f'!ecembed {posted["id"]} <#{self.logs_channel}> {{"title":"y"}}', f'!embed editcustom {posted["id"]} <#{self.logs_channel}> {{"title":"z"}}'):
            with self.subTest(line=line):
                said = await self.typed(line)
                self.assertNotIn("valid JSON", said)
                self.assertNotIn(self.net.messages[int(posted["id"])]["embeds"][0]["title"], ("x",))

    async def test_a_channel_that_isnt_here_refuses(self):
        said = await self.typed(f"!cembed <#{new_id()}> {self.JSON}")
        self.assertIn("isn't in this server", said)

    async def test_the_short_form_names_a_channel(self):
        await self.typed(f"!embed <#{self.logs_channel}> FF0000 Title | Description")
        self.assertEqual(self.embeds_in(self.logs_channel)[0]["embeds"][0]["title"], "Title")


class ServerRulesTest(Typed):
    def overwrites(self) -> dict:
        return self.cfg(COMMANDS_MODULE)["overwrites"]

    async def test_several_names_at_once(self):
        said = await self.typed("!disable ping about")
        self.assertIn("`ping`, `about` are now off", said)
        self.assertFalse(self.overwrites()["ping"]["enabled"])
        self.assertFalse(self.overwrites()["about"]["enabled"])
        await self.typed("!enable ping about")
        self.assertTrue(self.overwrites()["ping"]["enabled"])
        self.assertTrue(self.overwrites()["about"]["enabled"])

    async def test_the_longer_spelling_with_the_word_command(self):
        await self.typed("!config disable command ping")
        self.assertFalse(self.overwrites()["ping"]["enabled"])
        await self.typed("!config enable command ping")
        self.assertTrue(self.overwrites()["ping"]["enabled"])

    async def test_a_name_that_isnt_a_command_changes_none_of_them(self):
        said = await self.typed("!disable ping zzz")
        self.assertIn("There is no command called `zzz`", said)
        self.assertNotIn("ping", self.overwrites())

    async def test_all_and_mod(self):
        await self.typed("!disable mod")
        self.assertFalse(self.overwrites()["ban"]["enabled"])
        self.assertFalse(self.overwrites()["purge"]["enabled"])
        self.assertNotIn("ping", self.overwrites())
        await self.typed("!enable mod")
        self.assertTrue(self.overwrites()["ban"]["enabled"])
        said = await self.typed("!disable all")
        self.assertIn("are now off", said)
        self.assertFalse(self.overwrites()["ping"]["enabled"])
        for way_back in ("config", "dashboard", "my_data"):
            self.assertNotIn(way_back, self.overwrites())
        await self.typed("!enable all")
        self.assertTrue(self.overwrites()["ping"]["enabled"])

    async def test_animal_stands_for_the_picture_command(self):
        await self.typed("!disable animal")
        self.assertFalse(self.overwrites()["cute"]["enabled"])
        await self.typed("!enable animal")
        self.assertTrue(self.overwrites()["cute"]["enabled"])

    async def test_enable_list_shows_on_and_off(self):
        await self.typed("!disable ping")
        said = await self.typed("!enable list")
        on = said[said.index("On "):said.index("Off ")]
        off = said[said.index("Off "):]
        self.assertIn("`ping`", off)
        self.assertNotIn("`ping`", on)
        self.assertIn("`about`", on)

    async def test_modonly_and_restrict_take_all(self):
        await self.typed("!modonly all")
        self.assertTrue(self.overwrites()["ping"]["mod_only"])
        await self.typed("!unmodonly all")
        self.assertFalse(self.overwrites()["ping"]["mod_only"])
        await self.typed("!config restrict all")
        self.assertTrue(self.overwrites()["ping"]["restricted"])
        await self.typed("!config unrestrict all")
        self.assertFalse(self.overwrites()["ping"]["restricted"])

    async def test_ignore_several_channels(self):
        said = await self.typed(f"!ignore <#{self.general}> <#{self.logs_channel}>")
        self.assertIn(f"<#{self.logs_channel}>", said)
        self.assertEqual(sorted(self.cfg(COMMANDS_MODULE)["channel_bl"]), sorted([int(self.general), int(self.logs_channel)]))
        await self.typed(f"!unignore <#{self.general}> <#{self.logs_channel}>")
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [])

    async def test_a_mistyped_channel_ignores_nothing(self):
        said = await self.typed("!ignore gneral")
        self.assertIn("I can't find a channel called `gneral`", said)
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [])

    async def test_no_channel_means_this_one(self):
        await self.typed("!ignore")
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [int(self.general)])

    async def test_ignore_channel_is_the_longer_spelling(self):
        await self.typed(f"!config ignore channel <#{self.logs_channel}>")
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [int(self.logs_channel)])
        await self.typed(f"!config unignore channel <#{self.logs_channel}>")
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [])

    async def test_ignore_all_and_unignore_all(self):
        await self.typed("!ignore all")
        self.assertEqual(sorted(self.cfg(COMMANDS_MODULE)["channel_bl"]), sorted(channel.id for channel in self.guild.text_channels))
        await self.typed("!unignore all")
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [])

    async def test_ignore_server_is_the_setting_for_the_whole_bot(self):
        await self.typed("!ignore server")
        self.assertTrue(self.cfg(COMMANDS_MODULE)["mod_only"])
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [])
        await self.typed("!unignore server")
        self.assertFalse(self.cfg(COMMANDS_MODULE)["mod_only"])

    async def test_ignore_all_still_means_the_keyword_with_a_channel_called_all(self):
        self.guild.get_channel(int(self.logs_channel)).name = "all"
        await self.typed("!ignore all")
        self.assertEqual(len(self.cfg(COMMANDS_MODULE)["channel_bl"]), len(self.guild.text_channels))
        await self.typed("!unignore all")
        await self.typed(f"!ignore <#{self.logs_channel}>")
        self.assertEqual(self.cfg(COMMANDS_MODULE)["channel_bl"], [int(self.logs_channel)])

    async def test_modrole_clear_and_show(self):
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"mod_role": int(self.role_member["id"])})
        self.assertIn(self.role_member["id"], await self.typed("!config modrole"))
        await self.typed("!modrole clear")
        self.assertIsNone(self.cfg(COMMANDS_MODULE)["mod_role"])

    async def test_short_names_reach_the_real_command(self):
        for line, root in (("!disable cat", "cute"), ("!disable 8ball", "fun"), ("!restrict info", "members"), ("!modonly info", "members")):
            with self.subTest(line=line):
                said = await self.typed(line)
                self.assertNotIn("no command", said)
                self.assertIn(f"`{root}", said)
                self.assertIn(root, self.overwrites())

    async def test_one_short_word_for_a_subcommand_turns_off_only_that(self):
        await self.typed("!disable info")
        self.assertEqual(self.overwrites()["members"]["disabled_subcommands"], ["members info"])
        self.assertNotEqual(self.overwrites()["members"].get("enabled"), False)

    async def test_a_subcommand_turns_off_alone(self):
        said = await self.typed("!disable tag create")
        self.assertIn("`tag create`", said)
        self.assertNotEqual(self.overwrites()["tag"].get("enabled"), False)
        self.assertEqual(self.overwrites()["tag"]["disabled_subcommands"], ["tag create"])
        self.assertIn("turned off", await self.typed("!tag create a b"))
        self.assertNotIn("turned off", await self.typed("!tag list"))
        await self.typed("!enable tag create")
        self.assertEqual(self.overwrites()["tag"]["disabled_subcommands"], [])

    async def test_a_groups_own_function_turns_off_under_its_slash_name(self):
        await self.typed("!disable hl show")
        self.assertEqual(self.overwrites()["highlight"]["disabled_subcommands"], ["highlight show"])

    async def test_a_rule_that_covers_a_whole_command_refuses_a_subcommand(self):
        said = await self.typed("!modonly tag create")
        self.assertIn("name `tag`", said)
        self.assertNotIn("tag", self.overwrites())

    async def test_enabling_a_whole_command_brings_its_subcommands_back(self):
        await self.typed("!disable tag create")
        await self.typed("!enable tag")
        self.assertEqual(self.overwrites()["tag"]["disabled_subcommands"], [])

    async def test_the_way_back_in_stays_on(self):
        for line in ("!disable config", "!disable config enable"):
            with self.subTest(line=line):
                said = await self.typed(line)
                self.assertIn("way back in", said)
                self.assertNotIn("config", self.overwrites())

    async def test_diagnose_and_help_know_short_names(self):
        self.assertIn("automod slowmode", await self.typed("!diagnose slowmode"))
        self.assertIn("/automod slowmode", await self.typed("!help slowmode"))
        self.assertIn("/cute", await self.typed("!help cat"))

    async def test_a_tag_runs_blocks_by_the_long_spelling(self):
        self.assertEqual(named_command(self.bot, "lvl bl")[0].qualified_name, "level blacklist add")
        self.assertIsNone(named_command(self.bot, "ping about"))
        self.assertIsNone(named_command(self.bot, "cat food"))
        self.assertEqual(named_command(self.bot, "censor add")[0].qualified_name, "automod censor words")
        self.assertEqual(named_command(self.bot, "lvl resetall")[0].qualified_name, "level reset")
        self.assertEqual(named_command(self.bot, "cat")[0].qualified_name, "cute")


class ReportFormTest(Typed):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "reports", {"report_channel": int(self.logs_channel)})

    async def test_a_line_that_names_no_one_is_still_a_report(self):
        await self.typed("!report bob is spamming here", user_id=self.member_id)
        row = await self.db.one("SELECT * FROM reports WHERE guild_id = ?", (self.guild_id,))
        self.assertEqual(row["kind"], "place")
        self.assertEqual(row["target_id"], 0)
        self.assertEqual(row["reason"], "bob is spamming here")
        self.assertIn(f"<#{self.general}>", row["post_text"])

    async def test_its_link_points_at_the_message_above_it(self):
        above = self.net.message_json(int(self.general), {"content": "the problem"})
        self.net.messages[int(above["id"])] = above
        await self.typed("!report that was out of line", user_id=self.member_id)
        row = await self.db.one("SELECT * FROM reports WHERE guild_id = ?", (self.guild_id,))
        self.assertTrue(row["jump_url"].endswith(above["id"]), row["jump_url"])

    async def test_its_post_has_no_history_button(self):
        await self.typed("!report something is wrong", user_id=self.member_id)
        post = next(m for m in self.net.posted if m["channel_id"] == self.logs_channel)
        labels = [part.get("label") for row in post["components"] for part in row["components"]]
        self.assertIn("Actioned", labels)
        self.assertNotIn("View Message History", labels)

    async def test_two_such_reports_stay_apart(self):
        await self.typed("!report one thing", user_id=self.member_id)
        with mock.patch.object(self.bot.module("reports"), "_cooldown_left", return_value=0):
            await self.typed("!report another thing", user_id=self.mod_id)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM reports WHERE guild_id = ?", (self.guild_id,)), 2)

    async def test_no_review_opens(self):
        await self.bot.settings.save(self.guild_id, "reports", {"auto_review": True})
        with mock.patch.object(self.bot.module("reports"), "open_review", mock.AsyncMock()) as opened:
            await self.typed("!report something is wrong", user_id=self.member_id)
        opened.assert_not_awaited()

    async def test_naming_a_member_still_reports_them(self):
        await self.typed(f"!report <@{self.mod_id}> rude", user_id=self.member_id)
        row = await self.db.one("SELECT * FROM reports WHERE guild_id = ?", (self.guild_id,))
        self.assertEqual((row["kind"], row["target_id"], row["reason"]), ("user", self.mod_id, "rude"))

    async def test_a_member_with_no_words_is_asked_for_them(self):
        said = await self.typed(f"!report <@{self.mod_id}>", user_id=self.member_id)
        self.assertIn("Write what happened after their name", said)
        self.assertIsNone(await self.db.one("SELECT * FROM reports WHERE guild_id = ?", (self.guild_id,)))

    async def test_nothing_at_all_is_asked_for_words(self):
        said = await self.typed("!report", user_id=self.member_id)
        self.assertIn("Write what happened", said)


class LevelFormsTest(Typed):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.settings.save(self.guild_id, "levels", {"enabled": True})

    async def test_reset_with_a_member_first(self):
        levels = self.bot.module("levels")
        with mock.patch.object(levels, "reset_member", mock.AsyncMock(return_value=True)) as reset:
            await self.typed(f"!level reset {self.member_mention}")
        reset.assert_awaited_once_with(self.guild_id, self.member_id)

    async def test_reset_with_a_word_that_matches_no_one_changes_nothing(self):
        levels = self.bot.module("levels")
        with mock.patch.object(levels, "reset_member", mock.AsyncMock(return_value=True)) as reset:
            said = await self.typed("!level reset zzz")
        reset.assert_not_awaited()
        self.assertIn("zzz", said)

    async def test_message_xp(self):
        await self.typed("!lvl msgxp 30")
        self.assertEqual(self.cfg("levels")["custom_xp"], 30)
        await self.typed("!lvl msgxp")
        self.assertIsNone(self.cfg("levels")["custom_xp"])
        await self.typed("!level set_message_xp 40")
        self.assertEqual(self.cfg("levels")["custom_xp"], 40)
        await self.typed("!level set_message_xp reset")
        self.assertIsNone(self.cfg("levels")["custom_xp"])


    async def test_leaderboard_under_the_group(self):
        self.assertEqual(expand_line(self.bot, "level leaderboard 2"), "leaderboard 2")

    async def test_opacity_reads_zero_and_a_share_of_the_whole(self):
        await self.typed("!lvl alpha 0")
        self.assertEqual(self.cfg("levels")["card_opacity"], 0)
        await self.typed("!lvl alpha 1.0")
        self.assertEqual(self.cfg("levels")["card_opacity"], 100)
        await self.typed("!lvl opacity 40")
        self.assertEqual(self.cfg("levels")["card_opacity"], 40)


class BareGroupFormsTest(Typed):
    async def test_twitch_with_a_channel_and_text(self):
        notifications = self.bot.module("notifications")
        with mock.patch.object(notifications, "_twitch_lookup", mock.AsyncMock(return_value={"login": "tansmh", "id": "1", "display_name": "tansmh"})), mock.patch.object(notifications, "_twitch_store", mock.AsyncMock()) as store:
            await self.typed(f"!twitch tansmh <#{self.logs_channel}> {{name}} went live")
        args = store.await_args.args
        self.assertEqual(args[2], int(self.logs_channel))
        self.assertEqual(args[3], "{name} went live")

    async def test_a_mistyped_channel_doesnt_become_the_alert_text(self):
        notifications = self.bot.module("notifications")
        with mock.patch.object(notifications, "_twitch_store", mock.AsyncMock()) as store, mock.patch.object(notifications, "_twitch_lookup", mock.AsyncMock(return_value={"login": "x", "id": "1"})):
            said = await self.typed("!twitch add x gneral")
        store.assert_not_awaited()
        self.assertIn("I can't find a channel called `gneral`", said)

    async def test_a_role_can_end_the_alert_text(self):
        notifications = self.bot.module("notifications")
        with mock.patch.object(notifications, "_twitch_lookup", mock.AsyncMock(return_value={"login": "x", "id": "1"})), mock.patch.object(notifications, "_twitch_store", mock.AsyncMock()) as store:
            await self.typed(f"!twitch add x <#{self.logs_channel}> {{name}} is on {self.role_mention}")
        self.assertEqual(store.await_args.args[3], "{name} is on")
        self.assertEqual(store.await_args.args[4], int(self.role_member["id"]))

    async def test_youtube_from_the_top(self):
        notifications = self.bot.module("notifications")
        with mock.patch.object(notifications, "_youtube_resolve", mock.AsyncMock(return_value=("UC1", "Chan"))), mock.patch.object(notifications, "_youtube_store", mock.AsyncMock()) as store:
            await self.typed("!yt https://youtube.com/ygaming")
        self.assertEqual(store.await_args.args[:2], (self.guild_id, "UC1"))

    async def test_giveaway_from_the_top(self):
        await self.typed(f"!giveaway 1h10m 2 Nitro <#{self.logs_channel}>")
        row = await self.db.one("SELECT * FROM giveaways")
        self.assertEqual((row["prize"], row["winners"], row["channel_id"]), ("Nitro", 2, int(self.logs_channel)))

    async def test_sticky_with_the_channel_and_length_first(self):
        await self.typed(f"!sticky <#{self.logs_channel}> 1h No Spam")
        row = await self.db.one("SELECT * FROM sticky_messages")
        self.assertEqual(row["channel_id"], int(self.logs_channel))
        self.assertEqual(row["seconds"], 3600)
        self.assertIn("No Spam", row["summary"])

    async def test_a_sticky_that_opens_with_a_number_or_a_word_for_time_stays_whole(self):
        for line in ("5 ways to help", "week of events"):
            with self.subTest(line=line):
                await self.typed(f"!sticky {line}")
                row = await self.db.one("SELECT * FROM sticky_messages ORDER BY created_at DESC")
                self.assertIn(line, row["summary"])
                await self.typed("!sticky remove")

    async def test_sticky_template_with_the_channel_and_length_first(self):
        sticky = self.bot.module("sticky")
        stand_in = mock.Mock(callback=mock.AsyncMock())
        with mock.patch.object(sticky, "sticky_template", stand_in):
            await self.typed(f"!stickytemplate <#{self.logs_channel}> 1h Stick")
        args = stand_in.callback.await_args.args
        self.assertEqual(args[2], "Stick")
        self.assertEqual(args[3], 3600)
        self.assertEqual(args[4].id, int(self.logs_channel))

    async def test_starboard_from_the_top(self):
        await self.typed(f"!star <#{self.logs_channel}>")
        self.assertEqual(self.cfg("starboard")["channel_id"], int(self.logs_channel))

    async def test_star_emoji_with_just_the_emoji(self):
        await self.typed("!star emoji \N{FIRE}")
        self.assertEqual(self.cfg("starboard")["emoji"], "\N{FIRE}")
        await self.typed("!star emoji reset")
        self.assertIsNone(self.cfg("starboard")["emoji"])

    async def test_game_alerts_in_plain_words(self):
        said = await self.typed(f"!games alerts enable <#{self.logs_channel}>")
        self.assertNotIn("no option", said)
        self.assertTrue(self.cfg("notifications")["alerts_enabled"])
        self.assertEqual(self.cfg("notifications")["alert_channel"], int(self.logs_channel))
        await self.typed("!games alerts disable")
        self.assertFalse(self.cfg("notifications")["alerts_enabled"])

    async def test_triggers_ignore_takes_a_mix_in_either_order(self):
        await self.typed(f"!triggers ignore {self.member_mention} <#{self.general}>")
        self.assertEqual(self.cfg("triggers")["ignored_members"], [self.member_id])
        self.assertEqual(self.cfg("triggers")["ignored_channels"], [int(self.general)])
        await self.typed(f"!triggers unignore <#{self.general}> {self.member_mention}")
        self.assertEqual(self.cfg("triggers")["ignored_members"], [])
        self.assertEqual(self.cfg("triggers")["ignored_channels"], [])

    async def test_triggers_ignore_refuses_a_name_it_cant_find(self):
        said = await self.typed(f"!triggers ignore <#{self.general}> zzz")
        self.assertIn("zzz", said)
        self.assertEqual(self.cfg("triggers")["ignored_channels"], [])

    async def test_the_word_its_slash_form_goes_by_shows_the_listing(self):
        self.assertIn("Twitch alerts", await self.typed("!twitch list"))
        self.assertIn("No timed roles", await self.typed("!timedrole show"))


class SetChannelTest(Typed):
    async def test_set_welcome_sets_all_three_and_clears_all_three(self):
        said = await self.typed(f"!set welcome <#{self.logs_channel}>")
        self.assertIn(f"<#{self.logs_channel}>", said)
        cfg = self.cfg("greetings")
        self.assertEqual({cfg["welcome_channel"], cfg["farewell_channel"], cfg["ban_channel"]}, {int(self.logs_channel)})
        await self.typed("!set welcome")
        cfg = self.cfg("greetings")
        self.assertEqual({cfg["welcome_channel"], cfg["farewell_channel"], cfg["ban_channel"]}, {None})

    async def test_set_farewell_leaves_welcome_alone(self):
        await self.typed(f"!set welcome <#{self.general}>")
        await self.typed(f"!set farewell <#{self.logs_channel}>")
        cfg = self.cfg("greetings")
        self.assertEqual(cfg["welcome_channel"], int(self.general))
        self.assertEqual({cfg["farewell_channel"], cfg["ban_channel"]}, {int(self.logs_channel)})

    async def test_a_mistyped_channel_clears_nothing(self):
        await self.typed(f"!set welcome <#{self.logs_channel}>")
        said = await self.typed("!set welcome gneral")
        self.assertIn("I can't find a channel called `gneral`", said)
        self.assertEqual(self.cfg("greetings")["welcome_channel"], int(self.logs_channel))

    async def test_set_alone_says_what_it_takes(self):
        for line in ("!set", "!set zzz"):
            with self.subTest(line=line):
                self.assertIn("set welcome", await self.typed(line))

    async def test_set_needs_the_same_permission_as_the_setting(self):
        await self.typed(f"!set welcome <#{self.logs_channel}>", user_id=self.member_id)
        self.assertIsNone(self.cfg("greetings")["welcome_channel"])

    async def test_set_twitch_is_where_new_alerts_go(self):
        await self.typed(f"!set twitch <#{self.logs_channel}>")
        self.assertEqual(self.cfg("notifications")["twitch_channel"], int(self.logs_channel))
        notifications = self.bot.module("notifications")
        with mock.patch.object(notifications, "_twitch_lookup", mock.AsyncMock(return_value={"login": "x", "id": "1"})), mock.patch.object(notifications, "_twitch_store", mock.AsyncMock()) as store:
            await self.typed("!twitch add x")
        self.assertEqual(store.await_args.args[2], int(self.logs_channel))
        await self.typed("!set twitch")
        self.assertIsNone(self.cfg("notifications")["twitch_channel"])


class CleanConfigTest(Typed):
    async def test_it_drops_what_no_longer_exists_and_keeps_the_rest(self):
        dead_channel, dead_role = new_id(), new_id()
        live_channel, live_role = int(self.general), int(self.role_member["id"])
        await self.bot.settings.save(self.guild_id, "logging", {"default_channel": dead_channel, "ignored_channels": [dead_channel, live_channel], "ignored_members": [dead_channel]})
        await self.bot.settings.save(self.guild_id, "autoroles", {"blacklist": [dead_role, live_role]})
        await self.bot.settings.save(self.guild_id, "automod", {"allowed_roles": [dead_role], "honeypot": {"channels": [dead_channel, live_channel]}})
        await self.bot.settings.save(self.guild_id, COMMANDS_MODULE, {"mod_role": dead_role, "channel_bl": [dead_channel], "overwrites": {"ping": {"role_wl": [dead_role, live_role], "channel_bl": [dead_channel], "redirect": dead_channel}}})
        gone = discord.NotFound(FakeHTTPResponse(404), {"code": 10003, "message": "Unknown Channel"})
        with mock.patch.object(type(self.guild), "fetch_channel", mock.AsyncMock(side_effect=gone)):
            said = await self.typed("!cleanconfig")
        self.assertIn("logging", said)
        logs = self.cfg("logging")
        self.assertIsNone(logs["default_channel"])
        self.assertEqual(logs["ignored_channels"], [live_channel])
        self.assertEqual(logs["ignored_members"], [dead_channel], "an ID in a field about members can't be judged as a channel")
        self.assertEqual(self.cfg("autoroles")["blacklist"], [live_role])
        self.assertEqual(self.cfg("automod")["allowed_roles"], [])
        self.assertEqual(self.cfg("automod")["honeypot"]["channels"], [live_channel])
        rules = self.cfg(COMMANDS_MODULE)
        self.assertIsNone(rules["mod_role"])
        self.assertEqual(rules["channel_bl"], [])
        self.assertEqual(rules["overwrites"]["ping"]["role_wl"], [live_role])
        self.assertEqual(rules["overwrites"]["ping"]["channel_bl"], [])
        self.assertIsNone(rules["overwrites"]["ping"]["redirect"])

    async def test_nothing_to_clean_says_so(self):
        self.assertIn("still exists", await self.typed("!cleanconfig"))

    async def test_a_channel_discord_still_has_stays(self):
        thread = new_id()
        await self.bot.settings.save(self.guild_id, "logging", {"ignored_channels": [thread]})
        with mock.patch.object(type(self.guild), "fetch_channel", mock.AsyncMock(return_value=object())):
            await self.typed("!cleanconfig")
        self.assertEqual(self.cfg("logging")["ignored_channels"], [thread])

    async def test_a_channel_discord_wont_answer_about_stays(self):
        thread = new_id()
        await self.bot.settings.save(self.guild_id, "logging", {"ignored_channels": [thread]})
        down = discord.HTTPException(FakeHTTPResponse(500), {"code": 0, "message": "down"})
        with mock.patch.object(type(self.guild), "fetch_channel", mock.AsyncMock(side_effect=down)):
            await self.typed("!cleanconfig")
        self.assertEqual(self.cfg("logging")["ignored_channels"], [thread])

    async def test_it_needs_manage_server(self):
        await self.bot.settings.save(self.guild_id, "logging", {"default_channel": new_id()})
        await self.typed("!cleanconfig", user_id=self.member_id)
        self.assertIsNotNone(self.cfg("logging")["default_channel"])


class PurgeFormsTest(Typed):
    async def calls_to_purge(self, line: str) -> tuple:
        moderation = self.bot.module("moderation")
        with mock.patch.object(moderation, "_purge", mock.AsyncMock()) as purge:
            said = await self.typed(line)
        return purge.await_args, said

    async def test_the_count_defaults_to_100(self):
        for line in ("!purge bot", "!purge embeds", "!purge files", "!purge images", "!purge links", "!purge mentions", "!purge pings", "!purge human", "!purge humans", "!purge emoji"):
            with self.subTest(line=line):
                called, _ = await self.calls_to_purge(line)
                self.assertEqual(called.args[1], 100)

    async def test_all_keeps_asking_for_a_count(self):
        called, said = await self.calls_to_purge("!purge all")
        self.assertIsNone(called)
        self.assertIn("That needs a count", said)

    async def test_a_bare_purge_keeps_refusing(self):
        called, said = await self.calls_to_purge("!purge")
        self.assertIsNone(called)
        self.assertIn("Give a number", said)

    async def test_bot_takes_a_prefix_with_or_without_a_count(self):
        called, _ = await self.calls_to_purge("!purge bot 20 ?")
        self.assertEqual(called.args[1], 20)
        called, _ = await self.calls_to_purge("!purge bot ?")
        self.assertEqual(called.args[1], 100)
        check = called.args[2]
        self.assertTrue(check(mock.Mock(author=mock.Mock(bot=False), content="?help")))

    async def test_contains_takes_the_number_at_either_end(self):
        def holds(check, text):
            return check(mock.Mock(content=text))

        called, _ = await self.calls_to_purge("!purge contains 12 thanos")
        self.assertEqual(called.args[1], 12)
        self.assertTrue(holds(called.args[2], "THANOS did it"))
        called, _ = await self.calls_to_purge("!purge contains hello world 12")
        self.assertEqual(called.args[1], 12)
        self.assertTrue(holds(called.args[2], "say hello world now"))
        self.assertFalse(holds(called.args[2], "say hello now"))
        called, _ = await self.calls_to_purge("!purge contains thanos")
        self.assertEqual(called.args[1], 100)
        called, _ = await self.calls_to_purge('!purge contains "hello world"')
        self.assertTrue(holds(called.args[2], "say hello world now"))

    async def test_contains_on_slash_keeps_both_options(self):
        data = self.bot.get_command("purge contains").app_command.to_dict(self.bot.tree)
        self.assertEqual([(option["name"], option["required"]) for option in data["options"]], [("substring", True), ("search", False)])


class MassbanDaysTest(Typed):
    async def banned_with(self, line: str) -> tuple:
        moderation = self.bot.module("moderation")
        with mock.patch.object(moderation, "_bulk_ban", mock.AsyncMock(return_value=[])) as ban:
            said = await self.typed(line)
        return ban.await_args, said

    async def test_a_leading_number_is_days(self):
        called, _ = await self.banned_with(f"!massban 2 <@{new_id()}> <@{new_id()}>")
        self.assertEqual(len(called.args[1]), 2)
        self.assertEqual(called.args[3], 2 * 86400)

    async def test_no_number_uses_the_setting(self):
        called, _ = await self.banned_with(f"!massban <@{new_id()}>")
        self.assertEqual(called.args[3], self.cfg("moderation")["ban"]["days"] * 86400)

    async def test_an_id_isnt_a_number_of_days(self):
        someone = new_id()
        called, _ = await self.banned_with(f"!massban {someone} spammer")
        self.assertEqual([target.id for target in called.args[1]], [someone])

    async def test_a_number_past_seven_isnt_days(self):
        called, said = await self.banned_with("!massban 9 nothing")
        self.assertIsNone(called)
        self.assertTrue(said)

    async def test_slash_has_the_option(self):
        data = self.bot.get_command("massban").app_command.to_dict(self.bot.tree)
        days = next(option for option in data["options"] if option["name"] == "days")
        self.assertEqual(days["type"], discord.AppCommandOptionType.integer.value)


class PickerTest(Typed):
    """Typed forms read several words. The slash side keeps Discord's own pickers."""

    def options(self, *path: str) -> dict:
        command = next(c for c in self.bot.tree.get_commands() if c.name == path[0])
        for name in path[1:]:
            command = next(c for c in command.commands if c.name == name)
        return {option["name"]: option for option in command.to_dict(self.bot.tree)["options"]}

    async def test_timed_and_auto_roles_keep_the_role_picker(self):
        role_type = discord.AppCommandOptionType.role.value
        for path in (("timedrole", "add"), ("autorole", "blacklist"), ("autorole", "unblacklist")):
            with self.subTest(path=path):
                self.assertEqual(self.options(*path)["role"]["type"], role_type)

    async def test_the_rule_commands_keep_the_channel_and_role_pickers(self):
        self.assertEqual(self.options("config", "ignore")["channel"]["type"], discord.AppCommandOptionType.channel.value)
        self.assertEqual(self.options("config", "unignore")["channel"]["type"], discord.AppCommandOptionType.channel.value)
        self.assertEqual(self.options("config", "modrole")["role"]["type"], discord.AppCommandOptionType.role.value)
        self.assertEqual(self.options("twitch", "add")["channel"]["type"], discord.AppCommandOptionType.channel.value)
        self.assertEqual(self.options("twitch", "add")["role"]["type"], discord.AppCommandOptionType.role.value)
        self.assertEqual(self.options("report")["member"]["type"], discord.AppCommandOptionType.user.value)

    async def test_text_only_forms_stay_off_the_slash_list(self):
        for name in ("automod unwhitelist", "automod whitelist add", "greetings set_welcome", "greetings set_farewell", "twitch default_channel", "sticky fromtemplate", "level messagexp"):
            with self.subTest(name=name):
                self.assertIsNone(self.bot.get_command(name).app_command)

    async def test_timed_role_in_either_order(self):
        for line in ("!timedrole add 2h Member", "!timedrole add Member 2h", "!tr + 2h Member"):
            with self.subTest(line=line):
                await self.bot.settings.save(self.guild_id, "autoroles", {"timed": []})
                await self.typed(line)
                self.assertEqual([(row["role_id"], row["seconds"]) for row in self.cfg("autoroles")["timed"]], [(int(self.role_member["id"]), 7200)])

    async def test_a_timed_role_with_no_time_or_no_role(self):
        self.assertIn("length of time", await self.typed("!timedrole add Member"))
        self.assertIn("no role called", await self.typed("!timedrole add 2h zzz"))
        self.assertEqual(self.cfg("autoroles")["timed"], [])

    async def test_autorole_blacklist_takes_several(self):
        moderator = next(role for role in self.guild.roles if role.id != int(self.role_member["id"]) and not role.is_default())
        await self.typed(f'!autorole bl Member "{moderator.name}"')
        self.assertEqual(sorted(self.cfg("autoroles")["blacklist"]), sorted([int(self.role_member["id"]), moderator.id]))
        await self.typed(f'!autorole unblacklist "{moderator.name}" Member')
        self.assertEqual(self.cfg("autoroles")["blacklist"], [])

    async def test_a_quoted_name_of_several_words(self):
        role = self.guild.get_role(int(self.role_member["id"]))
        role.name = "fortnite expert"
        self.assertEqual(find_roles(self.guild, '"fortnite expert"', RoleNames(self.guild)), ([role], []))
        self.assertEqual(find_roles(self.guild, '"no such" zzz', RoleNames(self.guild)), ([], ["no such", "zzz"]))

    async def test_autorole_blacklist_refuses_a_name_it_cant_find(self):
        said = await self.typed("!autorole bl Member zzz")
        self.assertIn("zzz", said)
        self.assertEqual(self.cfg("autoroles")["blacklist"], [])

    async def test_autorole_blacklist_of_nothing(self):
        self.assertIn("Name at least one role", await self.typed("!autorole bl"))


class SmallerFormsTest(Typed):
    async def test_avatar_words(self):
        self.guild.get_member(self.member_id)._avatar = "a" * 32
        self.assertIn("account avatar", await self.typed(f"!avatar {self.member_mention} global"))
        self.assertIn("server avatar", await self.typed(f"!avatar {self.member_mention} server"))
        self.assertIn("avatar", await self.typed("!avatar global"))
        self.assertIn("I can't find a member called `zzz`", await self.typed("!avatar zzz"))

    async def test_echo_reads_a_leading_true_or_false(self):
        await self.typed(f"!echo <#{self.logs_channel}> false hello there")
        post = next(m for m in self.net.posted if m["channel_id"] == self.logs_channel)
        self.assertEqual(post["content"], "hello there")

    async def test_echo_refuses_a_channel_it_cant_find(self):
        said = await self.typed("!echo #gneral hello")
        self.assertIn("I can't find a channel called `#gneral`", said)

    async def test_a_tag_that_exists_offers_to_replace_it(self):
        await self.typed("!tag + t first", user_id=self.member_id)
        said = await self.typed("!tag + t again", user_id=self.member_id)
        self.assertIn("already a tag", said)
        offer = self.net.posted[-1]
        self.assertEqual(offer["components"][0]["components"][0]["label"], "Replace it")

    async def test_someone_else_gets_no_offer(self):
        await self.typed("!tag + t first", user_id=self.mod_id)
        await self.typed("!tag + t again", user_id=self.member_id)
        self.assertFalse(self.net.posted[-1].get("components"))

    async def test_a_trigger_with_no_response_says_what_it_needs(self):
        said = await self.typed('!triggers strict "its coming home"')
        self.assertIn("That needs a response.", said)

    async def test_defaults(self):
        self.assertEqual(self.cfg("greetings")["birthday_hour"], 0)
        self.assertEqual(self.bot.settings.schemas["core"].defaults()["prefixes"], ["!", "?"])
        said = await self.typed("?coinflip")
        self.assertRegex(said.lower(), "heads|tails")


    async def test_a_role_trigger_takes_a_whole_line(self):
        await self.typed(f"!triggers role Roles updated! <#{self.logs_channel}>")
        row = (await self.bot.module("triggers").rows(self.guild_id))[0]
        self.assertEqual(row["message"]["content"], "Roles updated!")
        self.assertEqual(row["channel_id"], int(self.logs_channel))


class HelpTest(Typed):
    async def test_typed_help_shows_the_prefix_and_other_names(self):
        said = await self.typed("!help triggers remove")
        self.assertIn("!triggers remove", said)
        self.assertIn("`del`", said)
        said = await self.typed("!help rr")
        self.assertIn("!reactionrole add", said)

    async def test_a_short_name_lists_on_its_command(self):
        said = await self.typed("!help automod slowmode")
        self.assertIn("`sm`", said)
        self.assertIn("slowmode", said)

    async def test_it_stays_within_discords_limits(self):
        await self.typed("!help automod")
        self.assert_no_errors()
        await self.typed("!help")
