import json
import time
import unittest

from captainguild.core import messages
from captainguild.core.durations import humanize, ordinal, parse_duration, split_leading_duration
from captainguild.core.perms import Who, evaluate, role_is_safe
from captainguild.core.validate import SECRET_MASK, Field, Invalid, Schema, ValidationError, boolean, integer, punishment, snowflake, snowflakes, text
from captainguild.web.api import safe_json
from captainguild.web.app import local_path
from tests.fakes import BotTest


class ValidateTest(unittest.TestCase):
    def test_snowflake_takes_text_or_int_and_refuses_the_rest(self):
        self.assertEqual(snowflake("111222333444555666"), 111222333444555666)
        self.assertEqual(snowflake(111222333444555666), 111222333444555666)
        for bad in ("abc", "12", True, None, "0123456789012345678"):
            with self.assertRaises(Invalid):
                snowflake(bad)

    def test_schema_keeps_base_values_and_refuses_unknown_fields(self):
        schema = Schema({"a": Field(1, integer(0, 5)), "b": Field("x", text(3)), "c": Schema({"d": Field(False, boolean)})})
        self.assertEqual(schema.defaults(), {"a": 1, "b": "x", "c": {"d": False}})
        self.assertEqual(schema.clean({"a": 2}, base={"a": 1, "b": "y", "c": {"d": True}}), {"a": 2, "b": "y", "c": {"d": True}})
        with self.assertRaises(ValidationError) as caught:
            schema.clean({"a": 9, "zz": 1, "c": {"d": "no"}})
        self.assertEqual(set(caught.exception.fields), {"a", "zz", "c.d"})

    def test_merged_gives_new_fields_their_default_and_drops_removed_ones(self):
        schema = Schema({"a": Field(1), "b": Field(2)})
        self.assertEqual(schema.merged({"a": 5, "old": 9}), {"a": 5, "b": 2})

    def test_secret_fields_never_leave_and_the_mask_keeps_the_value(self):
        schema = Schema({"key": Field("", text(50), secret=True), "other": Field(1, integer(0, 5))})
        self.assertEqual(schema.masked({"key": "abc", "other": 2}), {"key": SECRET_MASK, "other": 2})
        self.assertEqual(schema.masked({"key": "", "other": 2}), {"key": "", "other": 2})
        self.assertEqual(schema.clean({"key": SECRET_MASK, "other": 3}, base={"key": "abc", "other": 2}), {"key": "abc", "other": 3})
        self.assertEqual(schema.clean({"key": "new"}, base={"key": "abc", "other": 2})["key"], "new")

    def test_typing_or_pasting_after_the_mask_replaces_the_secret(self):
        # The page shows a saved secret as the mask in a password box, so a click and a paste lands after it.
        schema = Schema({"key": Field("", text(50), secret=True)})
        self.assertEqual(schema.clean({"key": SECRET_MASK + "abcd1234"}, base={"key": "abc"})["key"], "abcd1234")
        self.assertEqual(schema.clean({"key": "abcd1234" + SECRET_MASK}, base={"key": "abc"})["key"], "abcd1234" + SECRET_MASK)
        self.assertEqual(schema.clean({"key": ""}, base={"key": "abc"})["key"], "")

    def test_punishment_needs_a_duration_for_timed_actions(self):
        self.assertEqual(punishment({"actions": 1}), {"actions": 1, "seconds": 0, "purge_days": 0})
        with self.assertRaises(Invalid):
            punishment({"actions": 4, "seconds": 0})
        with self.assertRaises(Invalid):
            punishment({"actions": 1024, "seconds": 60 * 60 * 24 * 29})


class DurationTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_duration("3h42m"), 3 * 3600 + 42 * 60)
        self.assertEqual(parse_duration("2 days"), 172800)
        self.assertEqual(parse_duration("1w 3d"), 10 * 86400)
        self.assertEqual(parse_duration("1 hour and 5 minutes"), 3900)
        for bad in ("", "soon", "12", "3h spam"):
            self.assertIsNone(parse_duration(bad))

    def test_leading(self):
        self.assertEqual(split_leading_duration("3h spamming in general"), (10800, "spamming in general"))
        self.assertEqual(split_leading_duration("spamming"), (None, "spamming"))

    def test_humanize_and_ordinal(self):
        self.assertEqual(humanize(2 * 86400 + 23 * 3600 + 59 * 60), "2 days, 23 hours and 59 minutes")
        self.assertEqual(humanize(60), "1 minute")
        self.assertEqual(ordinal(43590), "43,590th")
        self.assertEqual(ordinal(43552), "43,552nd")
        self.assertEqual(ordinal(8101), "8,101st")
        self.assertEqual(ordinal(11), "11th")


class PermsTest(unittest.TestCase):
    ROLE = 111
    OTHER = 222
    CHAN = 333

    def run_rule(self, settings, *, required=(), roles=(), permissions=(), root="ban", leaf=None, channel=None, category=None, owner=False):
        who = Who(role_ids=set(roles), permissions=set(permissions), is_owner=owner)
        return evaluate(settings, root=root, leaf=leaf, required=required, who=who, channel_id=channel or self.CHAN, category_id=category)

    def test_needs_the_discord_permission(self):
        self.assertFalse(self.run_rule({}, required=["ban_members"]).allowed)
        self.assertTrue(self.run_rule({}, required=["ban_members"], permissions=["ban_members"]).allowed)

    def test_bypass_role_skips_the_permission(self):
        settings = {"overwrites": {"ban": {"bypass_roles": [self.ROLE]}}}
        self.assertTrue(self.run_rule(settings, required=["ban_members"], roles=[self.ROLE]).allowed)

    def test_manager_role_skips_every_permission(self):
        settings = {"mod_role": self.ROLE}
        self.assertTrue(self.run_rule(settings, required=["kick_members"], roles=[self.ROLE]).allowed)
        self.assertTrue(self.run_rule(settings, required=["ban_members"], roles=[self.ROLE]).allowed)

    def test_disabled_is_off_even_for_the_owner(self):
        settings = {"overwrites": {"ban": {"enabled": False}}}
        self.assertFalse(self.run_rule(settings, owner=True, permissions=["administrator"]).allowed)

    def test_disabled_subcommand(self):
        settings = {"overwrites": {"role": {"disabled_subcommands": ["role all"]}}}
        self.assertFalse(self.run_rule(settings, root="role", leaf="role all", permissions=["manage_guild"]).allowed)
        self.assertTrue(self.run_rule(settings, root="role", leaf="role add", permissions=["manage_guild"]).allowed)

    def test_ignored_channel_lets_manage_server_through(self):
        settings = {"channel_bl": [self.CHAN]}
        self.assertFalse(self.run_rule(settings).allowed)
        self.assertTrue(self.run_rule(settings, permissions=["manage_guild"]).allowed)

    def test_category_counts_as_the_place(self):
        settings = {"channel_bl": [444]}
        self.assertFalse(self.run_rule(settings, category=444).allowed)

    def test_allow_lists_and_their_skip(self):
        settings = {"overwrites": {"ban": {"channel_wl": [999]}}}
        self.assertFalse(self.run_rule(settings).allowed)
        self.assertTrue(self.run_rule({**settings, "channel_wl": [self.CHAN]}).allowed)
        self.assertTrue(self.run_rule({**settings, "role_wl": [self.ROLE]}, roles=[self.ROLE]).allowed)

    def test_role_lists(self):
        settings = {"overwrites": {"ban": {"role_wl": [self.ROLE]}}}
        self.assertFalse(self.run_rule(settings, roles=[self.OTHER]).allowed)
        self.assertTrue(self.run_rule(settings, roles=[self.ROLE]).allowed)
        settings = {"overwrites": {"ban": {"role_bl": [self.OTHER]}}}
        self.assertFalse(self.run_rule(settings, roles=[self.OTHER]).allowed)
        self.assertFalse(self.run_rule({"role_bl": [self.OTHER]}, roles=[self.OTHER]).allowed)

    def test_mod_only(self):
        settings = {"mod_only": True, "overwrites": {"tag": {"bypass_roles": [self.ROLE]}}}
        self.assertFalse(self.run_rule(settings, root="ping").allowed)
        self.assertTrue(self.run_rule(settings, root="tag", roles=[self.ROLE]).allowed)
        settings = {"overwrites": {"ping": {"mod_only": True}}}
        self.assertFalse(self.run_rule(settings, root="ping").allowed)
        self.assertTrue(self.run_rule(settings, root="ping", permissions=["manage_guild"]).allowed)

    def test_restricted_moves_output_to_the_bot_channel(self):
        settings = {"bot_channels": [555], "overwrites": {"ping": {"restricted": True}}}
        self.assertEqual(self.run_rule(settings, root="ping").restricted_channel, 555)
        self.assertIsNone(self.run_rule(settings, root="ping", channel=555).restricted_channel)
        self.assertIsNone(self.run_rule(settings, root="ping", permissions=["manage_guild"]).restricted_channel)

    def test_role_is_safe(self):
        self.assertTrue(role_is_safe(["send_messages"], {}))
        self.assertFalse(role_is_safe(["ban_members"], {}))
        self.assertTrue(role_is_safe(["ban_members"], {"elevated_roles": True}))


class MessagesTest(unittest.TestCase):
    def test_limits_are_discords_own(self):
        messages.clean_payload({"embeds": [{"description": "x" * 4096}]})
        with self.assertRaises(Invalid):
            messages.clean_payload({"embeds": [{"description": "x" * 4097}]})
        with self.assertRaises(Invalid):
            messages.clean_payload({"content": "x" * 2001})
        with self.assertRaises(Invalid):
            messages.clean_payload({"embeds": [{"title": "x" * 257}]})
        with self.assertRaises(Invalid):
            messages.clean_payload({"embeds": [{"description": "x" * 4000}, {"description": "x" * 2001}]})

    def test_links_must_be_http(self):
        with self.assertRaises(Invalid):
            messages.clean_payload({"embeds": [{"title": "a", "url": "javascript:alert(1)"}]})
        with self.assertRaises(Invalid):
            messages.clean_payload({"buttons": [{"label": "a", "url": "ftp://x"}]})

    def test_clean_drops_empty_embeds_and_unknown_keys(self):
        clean = messages.clean_payload({"content": "hi", "embeds": [{}, {"title": "t", "bogus": 1, "color": "#ff0000"}]})
        self.assertEqual(clean["embeds"], [{"title": "t", "color": 0xFF0000}])

    def test_legacy_shape(self):
        clean = messages.from_legacy("hello", json.dumps({"title": "t", "description": "d"}))
        self.assertEqual(clean["content"], "hello")
        self.assertEqual(clean["embeds"][0]["title"], "t")
        self.assertEqual(messages.from_legacy("x", "{}")["embeds"], [])

    def test_map_text_changes_everything_a_reader_sees(self):
        payload = {"content": "a", "embeds": [{"title": "a", "description": "a", "footer": {"text": "a"}, "author": {"name": "a"}, "fields": [{"name": "a", "value": "a"}]}], "buttons": []}
        out = messages.map_text(payload, lambda s: s.upper())
        self.assertEqual(out["content"], "A")
        self.assertEqual(out["embeds"][0]["footer"]["text"], "A")
        self.assertEqual(out["embeds"][0]["fields"][0]["value"], "A")
        self.assertEqual(payload["content"], "a")


class WebHelpersTest(unittest.TestCase):
    def test_big_ids_become_text(self):
        self.assertEqual(safe_json({"id": 111222333444555666, "n": 5, "list": [111222333444555666]}), {"id": "111222333444555666", "n": 5, "list": ["111222333444555666"]})

    def test_sign_in_never_redirects_off_the_site(self):
        for bad in ("https://evil.example", "//evil.example", "/\\evil.example", None, "", "/x\r\nSet-Cookie: a"):
            self.assertEqual(local_path(bad), "/servers")
        self.assertEqual(local_path("/dashboard/123/tags"), "/dashboard/123/tags")


class DatabaseTest(BotTest):
    async def test_transaction_rolls_back_on_error(self):
        await self.db.migrate("t", ["CREATE TABLE t (x INTEGER);"])
        with self.assertRaises(RuntimeError):
            async with self.db.transaction():
                await self.db.run("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM t"), 0)
        async with self.db.transaction():
            await self.db.run("INSERT INTO t VALUES (2)")
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM t"), 1)

    async def test_migrate_runs_each_step_once_and_in_order(self):
        steps = ["CREATE TABLE m (x INTEGER);", "INSERT INTO m VALUES (1);"]
        await self.db.migrate("m", steps)
        await self.db.migrate("m", steps)
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM m"), 1)
        await self.db.migrate("m", [*steps, "INSERT INTO m VALUES (2);"])
        self.assertEqual(await self.db.scalar("SELECT COUNT(*) FROM m"), 2)

    async def test_timer_retries_then_waits_an_hour_and_key_replaces(self):
        runs = []

        async def handler(payload, row):
            runs.append(payload)
            raise RuntimeError("nope")

        self.bot.timers.handler("x", handler)
        await self.bot.timers.schedule("x", time.time() - 1, {"a": 1}, key="k")
        await self.bot.timers.schedule("x", time.time() - 1, {"a": 2}, key="k")
        await self.bot.timers.run_due()
        self.assertEqual(runs, [{"a": 2}])
        row = await self.bot.timers.get_key("x", "k")
        self.assertEqual(row["tries"], 1)
        self.assertGreater(row["due_at"], time.time())
        await self.db.run("UPDATE timers SET tries = 7, due_at = 0")
        with self.assertLogs("captainguild.core.timers", level="ERROR"):
            await self.bot.timers.run_due()
        row = await self.bot.timers.get_key("x", "k")
        self.assertEqual(row["tries"], 8)
        self.assertAlmostEqual(row["due_at"], time.time() + 3600, delta=5)
