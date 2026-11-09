"""Who reaches the statistics pages. A server can open them to everyone in it or to anyone with the link, and that opens those routes and
nothing else. Public answers never carry a member's name or ID."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import discord

from captainguild.modules.statistics import SCHEMA, Statistics
from captainguild.modules.statistics_web import AREA_FIELDS, AREAS, NOT_PUBLIC
from captainguild.web.api import Router
from captainguild.web.app import DEV_SESSION_COOKIE
from tests.fakes import FakeChannel, FakePermissions
from tests.stats_seed_web import LEAVER, seed_small
from tests.test_authz_matrix import NO_ACCESS, SESSION_KEY, MatrixCase, filled, routes_of

NO_DRILL = "Analytics, drilldowns and CSV downloads take a role the statistics settings name, with Administrator, Manage Server or View Server Insights."


class Hidden(FakeChannel):
    """A channel that the person `hidden_from` and @everyone can't see."""

    def __init__(self, guild, name, hidden_from):
        super().__init__(guild, name)
        self.hidden_from = hidden_from

    def permissions_for(self, target):
        if target is self.hidden_from or target is self.guild.default_role:
            return FakePermissions()
        return super().permissions_for(target)


class Case(MatrixCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stats = self.cog(Statistics)
        self.help = FakeChannel(self.guild, "help")
        self.lounge = FakeChannel(self.guild, "Lounge", discord.ChannelType.voice)
        self.secret = Hidden(self.guild, "secret-plans", self.member)
        await self.save({"enabled": True})
        self.seed = await seed_small(self.bot, self.guild, member=self.member, admin=self.admin, mod=self.mod, general=self.channel, help_channel=self.help,
                                     lounge=self.lounge)
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, ?, ?, 0, 1)",
                                      (self.guild.id, self.seed.now_slot - 2, self.secret.id, self.admin.id))
        await self.bot.message_db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type) VALUES (?, ?, 'secret-plans', 0)", (self.guild.id, self.secret.id))

    async def save(self, form):
        await self.bot.settings.save(self.guild.id, "statistics", form)
        self.stats._web_cache.forget(self.guild.id)

    def stats_routes(self):
        return [(method, path) for method, path, level in routes_of(self.bot) if level == "viewer"]

    async def status(self, person, method, path, **kwargs):
        response = await self.call(await self.session(person), method, path, **kwargs)
        return response.status

    async def answer(self, person, method, path, **kwargs):
        response = await self.call(await self.session(person), method, path, **kwargs)
        return response.status, await response.json()


class Levels(Case):
    async def test_only_statistics_routes_ask_the_viewer_level(self):
        routes = self.stats_routes()
        self.assertGreater(len(routes), 20)
        # The counter channel and stat role routes belong to statistics too, and each checks its own area before anything else.
        self.assertEqual([path for _, path in routes if not path.startswith(("/statistics/", "/counters", "/statroles"))], [])

    async def test_staff_by_default_and_a_plain_member_gets_nothing(self):
        for method, path in self.stats_routes():
            response = await self.call(await self.session(self.member), method, path, json={})
            self.assertEqual(response.status, 403, (method, path))
            self.assertEqual((await response.json())["error"], NO_ACCESS)
        self.assertEqual(await self.status(self.staff, "GET", "/statistics/activity"), 200)

    async def test_managers_only_shuts_staff_out(self):
        await self.save({"dashboard_access": "managers"})
        for method, path in self.stats_routes():
            response = await self.call(await self.session(self.staff), method, path, json={})
            self.assertEqual(response.status, 403, (method, path))
            self.assertEqual((await response.json())["error"], NO_ACCESS, (method, path))
        self.assertEqual(await self.status(self.manager, "GET", "/statistics/activity"), 200)

    async def test_a_viewer_role_opens_them_whatever_the_level(self):
        viewers = self.guild.role("Stats fans", 1)
        self.member.roles.append(viewers)
        await self.save({"dashboard_access": "managers", "viewer_roles": [viewers.id]})
        self.assertEqual(await self.status(self.member, "GET", "/statistics/overview"), 200)

    async def test_members_level_opens_the_pages_read_only_and_nothing_else(self):
        await self.save({"dashboard_access": "members"})
        for method, path in [("GET", "/statistics/overview"), ("GET", "/statistics/activity"), ("GET", "/statistics/top?by=members"), ("GET", "/statistics/emoji"),
                             ("GET", "/statistics/invites"), ("GET", "/statistics/status"), ("GET", "/statistics/info"), ("GET", "/statistics/context"),
                             ("GET", "/statistics/panels")]:
            self.assertEqual(await self.status(self.member, method, path), 200, path)
        self.assertEqual(await self.status(self.member, "GET", "/statistics/presets"), 403, "loading presets takes a role")
        for path in ("/statistics/analytics", "/statistics/analytics/unique", "/statistics/analytics/members", "/statistics/drilldown/members?ids=1"):
            status, body = await self.answer(self.member, "GET", path)
            self.assertEqual((status, body["error"]), (403, NO_DRILL), path)
        status, _ = await self.answer(self.member, "POST", "/statistics/presets", json={"name": "Mine", "query": {}})
        self.assertEqual(status, 403)
        for method, path, level in routes_of(self.bot):
            if level == "viewer":
                continue
            response = await self.call(await self.session(self.member), method, path, json={})
            self.assertEqual(response.status, 403, (method, path))

    async def test_a_drill_role_with_view_server_insights_opens_analytics_and_drilldowns(self):
        insights = self.guild.role("Insights", 1, "view_guild_insights")
        self.member.roles.append(insights)
        await self.save({"dashboard_access": "members"})
        self.assertEqual(await self.status(self.member, "GET", "/statistics/analytics"), 403, "View Server Insights alone opens nothing")
        await self.save({"drill_roles": [insights.id]})
        self.assertEqual(await self.status(self.member, "GET", "/statistics/analytics"), 200)
        self.assertEqual(await self.status(self.member, "GET", f"/statistics/drilldown/channels?ids={self.channel.id}"), 200)

    async def test_a_server_that_requires_2fa_keeps_drilldowns_from_accounts_without_it(self):
        from captainguild.web.api import User
        from captainguild.web.app import SITE, csrf_for
        self.guild.mfa_level = discord.MFALevel.require_2fa
        insights = self.guild.role("Insights", 1, "view_guild_insights")
        self.member.roles.append(insights)
        await self.save({"dashboard_access": "members", "drill_roles": [insights.id]})
        for person in (self.member, self.admin):
            status, body = await self.answer(person, "GET", "/statistics/context")
            self.assertEqual((status, body["can_drill"], body["needs_2fa"]), (200, False, True), person.name)
            self.assertEqual(await self.status(person, "GET", "/statistics/analytics"), 403, person.name)
        token = await self.app[SITE].sessions.create(User(self.member.id, self.member.name, None, mfa=True))
        with_2fa = {"Cookie": f"{DEV_SESSION_COOKIE}={token}", "X-CSRF-Token": csrf_for(token, SESSION_KEY)}
        self.assertEqual((await self.call(with_2fa, "GET", "/statistics/analytics")).status, 200)
        self.guild.mfa_level = discord.MFALevel.disabled
        self.assertEqual(await self.status(self.member, "GET", "/statistics/analytics"), 200)

    async def test_the_server_list_shows_a_server_whose_statistics_someone_can_view(self):
        headers = await self.session(self.member)
        listed = await (await self.site.get("/api/me", headers=headers)).json()
        self.assertEqual(listed["guilds"], [])
        await self.save({"dashboard_access": "members"})
        listed = await (await self.site.get("/api/me", headers=headers)).json()
        self.assertEqual([(entry["id"], entry["access"]) for entry in listed["guilds"]], [(str(self.guild.id), "viewer")])
        await self.save({"enabled": False})
        listed = await (await self.site.get("/api/me", headers=headers)).json()
        self.assertEqual(listed["guilds"], [], "with statistics off there's nothing to view")

    async def test_a_channel_the_viewer_cant_see_is_left_out(self):
        await self.save({"dashboard_access": "members"})
        _, mine = await self.answer(self.member, "GET", "/statistics/top?by=channels")
        names = [dict(zip(mine["columns"], row))["name"] for row in mine["rows"]]
        self.assertIn("general", names)
        self.assertNotIn("Hidden channel", names)
        self.assertNotIn("secret-plans", json.dumps(mine))
        self.assertNotIn(str(self.secret.id), json.dumps(mine))
        _, theirs = await self.answer(self.admin, "GET", "/statistics/top?by=channels")
        self.assertIn("secret-plans", [dict(zip(theirs["columns"], row))["name"] for row in theirs["rows"]])
        _, info = await self.answer(self.member, "GET", "/statistics/info")
        self.assertNotIn("secret-plans", json.dumps(info))


class HiddenCounts(Case):
    """Members and the public read the numbers of the channels they can see now, and nothing of the rest: no row, no count, no ID, and an
    address naming a hidden channel shows nothing of it. Staff read every channel's numbers."""

    async def total(self, person, tail=""):
        status, body = await self.answer(person, "GET", f"/statistics/activity?period=all&include=both{tail}")
        self.assertEqual(status, 200, body.get("error"))
        return body["total"]

    async def test_a_hidden_or_deleted_channels_messages_dont_count_for_a_member(self):
        await self.save({"dashboard_access": "members", "viewer_filters": True})
        everything = await self.total(self.admin)
        self.assertEqual(await self.total(self.staff), everything)
        self.assertEqual(everything - await self.total(self.member), 1 + 6)  # the hidden channel's one and the deleted channel's six
        _, overview = await self.answer(self.member, "GET", "/statistics/overview?period=all&include=both")
        self.assertEqual(overview["totals"]["messages_now"], everything - 7)

    async def test_an_address_naming_a_hidden_channel_shows_nothing_of_it(self):
        await self.save({"dashboard_access": "members", "viewer_filters": True})
        self.assertEqual(await self.total(self.member, f"&channels={self.secret.id}&channel_mode=allow"), 0)
        self.assertEqual(await self.total(self.admin, f"&channels={self.secret.id}&channel_mode=allow"), 1)
        _, info = await self.answer(self.member, "GET", "/statistics/info")
        self.assertNotIn(str(self.secret.id), json.dumps(info))

    async def test_a_drilldown_names_no_hidden_channel_as_where_someone_spoke_last(self):
        insights = self.guild.role("Insights", 1, "view_guild_insights")
        self.member.roles.append(insights)
        await self.save({"dashboard_access": "members", "drill_roles": [insights.id]})
        status, body = await self.answer(self.member, "GET", f"/statistics/drilldown/members?ids={self.admin.id}&period=all")
        self.assertEqual(status, 200, body.get("error"))
        text = json.dumps(body)
        self.assertNotIn(str(self.secret.id), text)
        self.assertNotIn("Hidden channel", text)
        self.assertEqual(body["members"][0]["last_message"]["channel"]["name"], "general")

    async def test_the_public_reads_the_channels_everyone_can_see(self):
        await self.save({"dashboard_access": "public", "viewer_filters": True})
        base = f"/api/public/g/{self.guild.id}/statistics"
        answer = await (await self.site.get(f"{base}/activity?period=all&include=both&channels={self.secret.id}&channel_mode=allow")).json()
        self.assertEqual(answer["total"], 0)
        _, staff = await self.answer(self.admin, "GET", "/statistics/heatmap?kind=messages&period=all&include=both")
        public = await (await self.site.get(f"{base}/heatmap?kind=messages&period=all&include=both")).json()
        self.assertLess(sum(map(sum, public["cells"])), sum(map(sum, staff["cells"])))


class RolePermissions(Case):
    """Role permissions. Analytics, drilldowns and CSV downloads take a role from drill_roles and Administrator, Manage
    Server or View Server Insights, and full access passes without one. The query bar takes viewer_filters, which opens it to everyone
    who sees the pages, or a role from filter_roles. Presets load for those and for preset_read_roles, and save for the presets area."""

    async def context(self, person):
        status, body = await self.answer(person, "GET", "/statistics/context")
        self.assertEqual(status, 200, body.get("error"))
        return body

    async def total(self, person, tail="period=all&include=both"):
        status, body = await self.answer(person, "GET", f"/statistics/activity?{tail}")
        self.assertEqual(status, 200, body.get("error"))
        return body["total"]

    async def test_a_fresh_server_keeps_the_query_bar_from_members(self):
        self.assertFalse(SCHEMA.defaults()["viewer_filters"])
        await self.save({"dashboard_access": "members"})
        default = await self.total(self.member, "")
        self.assertEqual(await self.total(self.member), default, "the address is ignored")
        self.assertFalse((await self.context(self.member))["can_filter"])
        self.assertTrue((await self.context(self.staff))["can_filter"])
        self.assertNotEqual(await self.total(self.staff), await self.total(self.staff, ""))

    async def test_a_filter_role_or_the_switch_opens_the_query_bar(self):
        filterers = self.guild.role("Filterers", 1)
        await self.save({"dashboard_access": "members", "filter_roles": [filterers.id]})
        default = await self.total(self.member, "")
        self.member.roles.append(filterers)
        self.assertNotEqual(await self.total(self.member), default)
        self.assertTrue((await self.context(self.member))["can_filter"])
        self.member.roles.remove(filterers)
        await self.save({"viewer_filters": True})
        self.assertNotEqual(await self.total(self.member), default)

    async def test_drilldowns_take_the_role_and_the_permission_at_every_level(self):
        insights = self.guild.role("Insights", 1, "view_guild_insights")
        drillers = self.guild.role("Drillers", 1)
        await self.save({"dashboard_access": "members", "drill_roles": [drillers.id]})
        for person in (self.member, self.staff):
            for roles, opened in (([insights], False), ([drillers], False), ([insights, drillers], True)):
                person.roles[:] = [role for role in person.roles if role not in (insights, drillers)] + roles
                context = await self.context(person)
                self.assertEqual((context["can_drill"], context["can_csv"]), (opened, opened), (person.name, [role.name for role in roles]))
                self.assertEqual(await self.status(person, "GET", "/statistics/analytics"), 200 if opened else 403, (person.name, [role.name for role in roles]))
            person.roles[:] = [role for role in person.roles if role not in (insights, drillers)]
        for person in (self.manager, self.admin):
            self.assertEqual(await self.status(person, "GET", "/statistics/analytics"), 200, person.name)
        self.member.roles += [insights, drillers]
        self.assertTrue((await self.context(self.member))["can_filter"], "a drill role changes the query bar too")

    async def test_without_the_drill_right_a_ranking_of_people_is_cut_short(self):
        from captainguild.modules import statistics_web
        insights = self.guild.role("Insights", 1, "view_guild_insights")
        await self.save({"dashboard_access": "members", "drill_roles": [insights.id]})
        limit = statistics_web.LIST_LIMIT
        statistics_web.LIST_LIMIT = 1
        try:
            status, body = await self.answer(self.member, "GET", "/statistics/top?by=members")
            self.assertEqual((status, len(body["rows"]), body["limited"]), (200, 1, True))
            status, body = await self.answer(self.member, "GET", "/statistics/invites")
            self.assertEqual((len(body["joins"]["rows"]), body["limited"]), (1, True))
            self.member.roles.append(insights)
            status, body = await self.answer(self.member, "GET", "/statistics/top?by=members")
            self.assertEqual((len(body["rows"]), body["limited"]), (2, False))  # the person who left wrote in the deleted channel
            status, body = await self.answer(self.admin, "GET", "/statistics/top?by=members")
            self.assertEqual(len(body["rows"]), 3)
        finally:
            statistics_web.LIST_LIMIT = limit

    async def test_presets_load_for_readers_and_save_for_the_presets_area(self):
        readers = self.guild.role("Readers", 1)
        keepers = self.guild.role("Keepers", 1)
        filterers = self.guild.role("Filterers", 1)
        await self.save({"dashboard_access": "members", "preset_read_roles": [readers.id], "area_roles": {"presets": [keepers.id]}, "filter_roles": [filterers.id]})
        status, preset = await self.answer(self.admin, "POST", "/statistics/presets", json={"name": "All of it", "query": {"period": "all", "include": "both"}})
        self.assertEqual(status, 200)
        self.assertEqual(await self.status(self.member, "GET", "/statistics/presets"), 403)
        for role, editing in ((readers, False), (filterers, False), (keepers, True)):
            self.member.roles.append(role)
            status, body = await self.answer(self.member, "GET", "/statistics/presets")
            self.assertEqual((status, [row["name"] for row in body.get("filters", [])], body.get("can_edit")), (200, ["All of it"], editing), role.name)
            self.member.roles.remove(role)
        default = await self.total(self.member, "")
        everything = await self.total(self.admin) - 7  # less the hidden channel's one and the deleted channel's six, which members don't read
        self.member.roles.append(readers)
        self.assertEqual(await self.total(self.member, f"preset={preset['id']}&period=7d"), everything, "a reader gets the preset's view and nothing else")
        self.assertEqual(await self.total(self.member, "period=all&include=both"), default)
        self.assertTrue((await self.context(self.member))["can_read_presets"])

    async def test_the_people_search_takes_the_drill_right_below_staff(self):
        insights = self.guild.role("Insights", 1, "view_guild_insights")
        await self.save({"dashboard_access": "members", "drill_roles": [insights.id]})
        self.assertEqual(await self.status(self.member, "GET", "/statistics/people?q=mem"), 403)
        self.member.roles.append(insights)
        self.assertEqual(await self.status(self.member, "GET", "/statistics/people?q=mem"), 200)
        self.assertEqual(await self.status(self.staff, "GET", "/statistics/people?q=mem"), 200)

    async def test_the_settings_page_holds_every_role_list(self):
        status, body = await self.answer(self.admin, "GET", "/statistics/settings")
        self.assertEqual(status, 200)
        for name in ("drill_roles", "filter_roles", "preset_read_roles", "viewer_roles", "override_roles", "area_roles", "viewer_filters"):
            self.assertIn(name, body["settings"])
        drillers = self.guild.role("Drillers", 1)
        status, _ = await self.answer(self.admin, "PUT", "/statistics/settings", json={"drill_roles": [drillers.id], "filter_roles": [drillers.id],
                                                                                           "preset_read_roles": [drillers.id]})
        self.assertEqual(status, 200)
        self.assertEqual(self.settings("statistics")["drill_roles"], [drillers.id])


class Strangers(Case):
    """Reading a server's settings keeps a merged copy of them for good, so a read for a server the bot isn't in must not read them."""

    async def test_public_reads_for_servers_the_bot_isnt_in_keep_nothing(self):
        before = len(self.bot.settings._merged)
        for number in range(40):
            response = await self.site.get(f"/api/public/g/{4_000_000_000_000_000_000 + number}/statistics/context")
            self.assertEqual(response.status, 404)
        self.assertEqual(len(self.bot.settings._merged), before)

    async def test_signed_in_reads_for_servers_the_bot_isnt_in_keep_nothing(self):
        headers = await self.session(self.member)
        before = len(self.bot.settings._merged)
        for number in range(40):
            response = await self.site.get(f"/api/g/{5_000_000_000_000_000_000 + number}/statistics/overview", headers=headers)
            self.assertEqual(response.status, 403)
        self.assertEqual(len(self.bot.settings._merged), before)


def _request_from(peer: str, forwarded: str | None = None):
    from unittest import mock

    from aiohttp.test_utils import make_mocked_request
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda name, default=None: (peer, 40000) if name == "peername" else default
    return make_mocked_request("GET", "/", headers={"X-Forwarded-For": forwarded} if forwarded is not None else {}, transport=transport)


class PublicVisitors(Case):
    """LiteSpeed on the same host passes every visitor on from 127.0.0.1, so a limit per address has to read the address it adds."""

    def test_the_address_the_proxy_added_names_the_visitor(self):
        from captainguild.web.app import visitor
        self.assertEqual(visitor(_request_from("127.0.0.1", "203.0.113.9")), "203.0.113.9")
        self.assertEqual(visitor(_request_from("127.0.0.1", "10.9.9.9, 203.0.113.9")), "203.0.113.9", "what the visitor wrote comes first, the proxy's last")
        self.assertEqual(visitor(_request_from("::1", "198.51.100.7")), "198.51.100.7")
        self.assertEqual(visitor(_request_from("127.0.0.1")), "127.0.0.1")
        self.assertEqual(visitor(_request_from("127.0.0.1", "not an address")), "127.0.0.1")

    def test_a_header_from_anywhere_but_the_proxy_is_ignored(self):
        from captainguild.web.app import visitor
        self.assertEqual(visitor(_request_from("198.51.100.20", "203.0.113.9")), "198.51.100.20")

    def test_one_address_of_a_network_counts_as_the_network(self):
        from captainguild.web.app import visitor
        first = visitor(_request_from("127.0.0.1", "2001:db8:1:2::5"))
        self.assertEqual(first, visitor(_request_from("127.0.0.1", "2001:db8:1:2:ffff::9")))
        self.assertNotEqual(first, visitor(_request_from("127.0.0.1", "2001:db8:1:3::5")))

    async def test_two_visitors_behind_the_proxy_have_their_own_limits(self):
        from captainguild.web.app import SITE, RateLimiter
        self.app[SITE].public_limiter = RateLimiter(5)
        await self.save({"dashboard_access": "public"})
        path = f"/api/public/g/{self.guild.id}/statistics/overview"
        for _ in range(5):
            self.assertEqual((await self.site.get(path, headers={"X-Forwarded-For": "203.0.113.9"})).status, 200)
        self.assertEqual((await self.site.get(path, headers={"X-Forwarded-For": "203.0.113.9"})).status, 429)
        self.assertEqual((await self.site.get(path, headers={"X-Forwarded-For": "198.51.100.7"})).status, 200)

    async def test_reads_that_miss_the_cache_open_a_few_connections_at_most(self):
        import asyncio
        import contextlib

        from captainguild.modules.statistics_web import READS_AT_ONCE
        await self.save({"dashboard_access": "public", "viewer_filters": True})
        db = self.bot.message_db
        opened = {"now": 0, "most": 0}
        real = db.reader

        @contextlib.asynccontextmanager
        async def counted():
            opened["now"] += 1
            opened["most"] = max(opened["most"], opened["now"])
            try:
                await asyncio.sleep(0.05)
                async with real() as reader:
                    yield reader
            finally:
                opened["now"] -= 1
        db.reader = counted
        paths = [f"/api/public/g/{self.guild.id}/statistics/activity?period={days}d" for days in range(1, 13)]
        answers = await asyncio.gather(*(self.site.get(path, headers={"X-Forwarded-For": f"203.0.113.{index}"}) for index, path in enumerate(paths)))
        self.assertEqual([answer.status for answer in answers], [200] * len(paths))
        self.assertEqual(opened["most"], READS_AT_ONCE)

    async def test_the_public_context_is_read_once_a_minute(self):
        await self.save({"dashboard_access": "public"})
        calls = []
        real = self.bot.ops.describe_guild
        self.bot.ops.describe_guild = lambda guild_id: calls.append(guild_id) or real(guild_id)
        for index in range(3):
            response = await self.site.get(f"/api/public/g/{self.guild.id}/statistics/context", headers={"X-Forwarded-For": f"203.0.113.{index}"})
            self.assertEqual(response.status, 200)
        self.assertEqual(len(calls), 1)


class DeletedChannels(Case):
    """The settings page promises members channel names only where Discord shows them the channel, and Discord shows nobody a deleted one."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.message_db.run("INSERT INTO stat_channels (guild_id, channel_id, name, type, deleted_at) VALUES (?, 888000000000000888, 'staff-appeals-jordan', 0, ?)",
                                      (self.guild.id, time.time() - 60))
        await self.bot.message_db.run("INSERT INTO stat_messages (guild_id, slot, channel_id, user_id, flags, count) VALUES (?, ?, 888000000000000888, ?, 0, 3)",
                                      (self.guild.id, self.seed.now_slot - 3, self.admin.id))
        await self.save({"dashboard_access": "members"})

    async def names(self, person):
        status, body = await self.answer(person, "GET", "/statistics/top?by=channels&period=all")
        self.assertEqual(status, 200, body.get("error"))
        names = [dict(zip(body["columns"], row))["name"] for row in body["rows"]]
        self.assertIsNone(next((name for name in ("staff-appeals-jordan", "old-chat") if name in json.dumps(body) and name not in names), None))
        return names

    async def test_a_view_only_member_sees_no_deleted_channels_name(self):
        seen = await self.names(self.member)
        self.assertNotIn("staff-appeals-jordan", seen)
        self.assertNotIn("old-chat", seen)

    async def test_staff_still_see_what_a_deleted_channel_was_called(self):
        seen = await self.names(self.staff)
        self.assertIn("staff-appeals-jordan", seen)
        self.assertIn("old-chat", seen)


class AreaHolders(Case):
    """Someone holding a role for any part of the statistics settings reaches the statistics routes at every access level while statistics
    are on: the settings page, and the counter channel and stat role routes, which check their own area."""

    async def test_every_area_opens_the_routes_at_every_level(self):
        holders = self.guild.role("Holders", 1)
        self.member.roles.append(holders)
        self.assertEqual(len(AREAS), 10)
        for area in AREAS:
            for access in ("managers", "staff", "members", "public"):
                await self.save({"dashboard_access": access, "area_roles": {area: [holders.id]}})
                self.assertEqual(self.stats.viewer_level(self.guild.id, self.member.id, None), "viewer", (area, access))

    async def test_a_counters_role_reaches_its_routes_at_the_managers_level(self):
        counters = self.guild.role("Counter keepers", 1)
        self.member.roles.append(counters)
        await self.save({"dashboard_access": "managers", "area_roles": {"counters": [counters.id]}})
        status, body = await self.answer(self.member, "GET", "/statistics/settings")
        self.assertEqual((status, body.get("areas")), (200, ["counters"]))
        self.assertEqual(await self.status(self.member, "GET", "/statistics/context"), 200)

    async def test_counter_and_stat_role_holders_get_links_to_their_pages(self):
        counters, statroles = self.guild.role("Counter keepers", 1), self.guild.role("Rule keepers", 1)
        self.member.roles.append(counters)
        await self.save({"dashboard_access": "managers", "area_roles": {"counters": [counters.id], "statroles": [statroles.id]}})
        status, body = await self.answer(self.member, "GET", "/statistics/context")
        self.assertEqual((status, body.get("can_counters"), body.get("can_statroles")), (200, True, False))
        self.member.roles.append(statroles)
        status, body = await self.answer(self.member, "GET", "/statistics/context")
        self.assertEqual((body.get("can_counters"), body.get("can_statroles")), (True, True))
        nav = (Path(__file__).resolve().parent.parent / "captainguild" / "web" / "static" / "js" / "nav.js").read_text(encoding="utf-8")
        for key in ("counters", "statroles"):
            self.assertIsNotNone(re.search(rf"key: '{key}'[^}}]*linked: true", nav), f"{key}: a viewer opens the page by its link")

    async def test_the_time_role_reaches_the_settings_at_the_default_level(self):
        timekeepers = self.guild.role("Timekeepers", 1)
        self.member.roles.append(timekeepers)
        await self.save({"area_roles": {"time": [timekeepers.id]}})
        status, body = await self.answer(self.member, "GET", "/statistics/settings")
        self.assertEqual((status, body.get("areas")), (200, ["time"]))

    async def test_an_area_holder_hands_out_no_power_through_a_role_list(self):
        keepers = self.guild.role("Dashboard keepers", 1)
        self.member.roles.append(keepers)
        await self.save({"dashboard_access": "members", "area_roles": {"dashboard": [keepers.id], "commands": [keepers.id]}})
        for name in ("drill_roles", "filter_roles", "preset_read_roles", "viewer_roles", "override_roles"):
            status, body = await self.answer(self.member, "PUT", "/statistics/settings", json={name: [keepers.id]})
            self.assertEqual((status, "Administrator" in body.get("error", "")), (400, True), name)
            self.assertEqual(self.settings("statistics")[name], [], name)
        status, body = await self.answer(self.member, "PUT", "/statistics/settings", json={"viewer_filters": True, "command_access": "restricted"})
        self.assertEqual(status, 200, body.get("error"))
        for name in ("drill_roles", "filter_roles", "preset_read_roles"):
            status, _ = await self.answer(self.admin, "PUT", "/statistics/settings", json={name: [self.guild.default_role.id]})
            self.assertEqual(status, 400, f"@everyone can't hold {name}")

    async def test_with_statistics_off_an_area_role_or_a_viewer_role_opens_nothing(self):
        holders = self.guild.role("Holders", 1)
        self.member.roles.append(holders)
        await self.save({"dashboard_access": "managers", "area_roles": {"time": [holders.id]}, "viewer_roles": [holders.id], "enabled": False})
        self.assertIsNone(self.stats.viewer_level(self.guild.id, self.member.id, None))
        self.assertEqual(await self.status(self.member, "GET", "/statistics/overview"), 403)
        listed = await (await self.site.get("/api/me", headers=await self.session(self.member))).json()
        self.assertEqual(listed["guilds"], [])


class Settings(Case):
    async def test_a_member_with_no_area_role_changes_nothing(self):
        await self.save({"dashboard_access": "members"})
        current = SCHEMA.merged(self.bot.settings.get(self.guild.id, "statistics"))
        for name in SCHEMA.fields:
            status, _ = await self.answer(self.member, "PUT", "/statistics/settings", json={name: current[name]})
            self.assertEqual(status, 403, name)
        self.assertEqual(await self.status(self.member, "GET", "/statistics/settings"), 403)

    async def test_an_area_role_changes_its_area_and_nothing_more(self):
        timekeepers = self.guild.role("Timekeepers", 1)
        self.member.roles.append(timekeepers)
        await self.save({"dashboard_access": "members", "area_roles": {"time": [timekeepers.id]}})
        status, body = await self.answer(self.member, "GET", "/statistics/settings")
        self.assertEqual((status, body["areas"], body["all"]), (200, ["time"], False))
        status, _ = await self.answer(self.member, "PUT", "/statistics/settings", json={"timezone": "Europe/Paris", "interval": "week"})
        self.assertEqual(status, 200)
        self.assertEqual(self.settings("statistics")["timezone"], "Europe/Paris")
        for name in [field for area, fields in AREA_FIELDS.items() if area != "time" for field in fields] + ["dashboard_access", "viewer_roles", "area_roles", "enabled"]:
            status, _ = await self.answer(self.member, "PUT", "/statistics/settings", json={name: self.settings("statistics")[name]})
            self.assertEqual(status, 403, name)

    async def test_administrator_fields_stay_with_administrators(self):
        for name, value in (("dashboard_access", "public"), ("viewer_roles", [self.role_member.id]), ("override_roles", [self.role_member.id]),
                            ("area_roles", {"time": [self.role_member.id]})):
            status, body = await self.answer(self.manager, "PUT", "/statistics/settings", json={name: value})
            self.assertEqual(status, 400, name)
            self.assertIn("Administrator", body["error"], name)
        status, _ = await self.answer(self.admin, "PUT", "/statistics/settings", json={"dashboard_access": "public"})
        self.assertEqual(status, 200)
        everyone = await self.answer(self.admin, "PUT", "/statistics/settings", json={"area_roles": {"presets": [self.guild.default_role.id]}})
        self.assertEqual(everyone[0], 400, "@everyone can't hold a settings area")

    async def test_a_preset_role_saves_and_deletes_presets(self):
        keepers = self.guild.role("Preset keepers", 1)
        self.member.roles.append(keepers)
        await self.save({"dashboard_access": "members", "area_roles": {"presets": [keepers.id]}})
        status, preset = await self.answer(self.member, "POST", "/statistics/presets", json={"name": "Mine", "query": {"period": "7d"}})
        self.assertEqual(status, 200)
        self.assertEqual(await self.status(self.member, "DELETE", f"/statistics/presets/{preset['id']}"), 200)


class Panels(Case):
    async def test_each_person_reads_and_writes_their_own_panels_only(self):
        await self.save({"dashboard_access": "members"})
        mine = [{"stat": "messages", "value": "sum", "period": "7d"}]
        self.assertEqual(await self.status(self.member, "PUT", "/statistics/panels", json={"panels": mine}), 200)
        _, theirs = await self.answer(self.mod, "GET", "/statistics/panels")
        self.assertEqual(theirs["panels"], [])
        status, _ = await self.answer(self.mod, "PUT", "/statistics/panels", json={"panels": [], "user_id": str(self.member.id)})
        self.assertEqual(status, 200)
        _, still = await self.answer(self.member, "GET", "/statistics/panels")
        self.assertEqual(len(still["panels"]), 1)
        _, values = await self.answer(self.mod, "GET", "/statistics/panels/values")
        self.assertEqual(values["values"], [])


class Public(Case):
    def public_routes(self):
        router = Router()
        self.stats.public_api(router)
        return [path for method, path, _, _ in router.routes]

    async def public(self, path):
        response = await self.site.get(f"/api/public/g/{self.guild.id}{filled(path)}")
        return response.status, await response.read()

    async def test_every_public_route_refuses_until_the_server_opens_it(self):
        paths = self.public_routes()
        self.assertGreater(len(paths), 6)
        for access in ("managers", "staff", "members"):
            await self.save({"dashboard_access": access})
            for path in paths:
                status, body = await self.public(path)
                self.assertEqual((status, json.loads(body)["error"]), (404, NOT_PUBLIC), (access, path))
        await self.save({"dashboard_access": "public", "enabled": False})
        for path in paths:
            self.assertEqual((await self.public(path))[0], 404, path)
        await self.save({"enabled": True})
        for path in paths:
            self.assertEqual((await self.public(path))[0], 200, path)

    async def test_a_public_answer_never_names_a_member(self):
        for person, name in ((self.member, "Zylophane"), (self.admin, "Quorvanta"), (self.mod, "Wexlerine")):
            person.name = name
            person.global_name = name
        await self.bot.message_db.run("UPDATE stat_people SET name = ?, nick = ? WHERE guild_id = ? AND user_id = ?", ("Zylophane", "Zylo Nick", self.guild.id, self.member.id))
        await self.save({"dashboard_access": "public"})
        secrets = [str(person.id) for person in self.guild.members] + [str(LEAVER), "Zylophane", "Quorvanta", "Wexlerine", "Zylo Nick", "Member Person", "Gone Person", "Boss", "leaver"]
        queries = ["", "?kind=voice", "?period=all&include=both", "?kind=status", f"?members={self.member.id}&member_mode=allow", f"?roles_allow={self.role_member.id}"]
        for path in self.public_routes():
            for tail in queries:
                status, body = await self.public(path + tail)
                if status != 200:
                    self.assertIn(status, (400, 403), (path, tail))  # a kind the route doesn't take, or the list of members
                    continue
                text = body.decode()
                for secret in secrets:
                    self.assertNotIn(secret, text, (path, tail, secret))
        status, _ = await self.public("/statistics/top?by=members")
        self.assertEqual(status, 403)

    async def test_member_filters_in_a_public_address_are_ignored(self):
        await self.save({"dashboard_access": "public"})
        status, body = await self.public(f"/statistics/activity?members={self.member.id}&member_mode=allow")
        self.assertEqual(json.loads(body)["total"], 14)  # everyone's in the channels everyone sees: the hidden channel's 1 and the deleted one's 6 are out

    async def test_public_channel_names_are_what_everyone_can_see(self):
        await self.save({"dashboard_access": "public"})
        status, body = await self.public("/statistics/top?by=channels&period=all")
        rows = [dict(zip(json.loads(body)["columns"], row)) for row in json.loads(body)["rows"]]
        self.assertFalse([row for row in rows if row["hidden"] or row["name"] == "Hidden channel"])
        self.assertNotIn(str(self.secret.id), body.decode())
        self.assertNotIn("secret-plans", body.decode())
        self.assertNotIn("old-chat", body.decode(), "a deleted channel isn't named to the public")
        self.assertIn("general", [row["name"] for row in rows])

    async def test_the_public_page_is_served_without_a_session(self):
        response = await self.site.get(f"/stats/{self.guild.id}")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    async def test_no_session_cookie_is_read(self):
        await self.save({"dashboard_access": "public"})
        response = await self.site.get(f"/api/public/g/{self.guild.id}/statistics/overview", headers={"Cookie": f"{DEV_SESSION_COOKIE}=nonsense"})
        self.assertEqual(response.status, 200)
