"""Forum messages on the dashboard: the forums and their tags for the page, and a settings save refusing what the commands refuse."""
from __future__ import annotations

from typing import Any

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core import access
from captainguild.modules import forummessages as fm
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.test_forummessages import Case


class PageTest(Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.forum_mod_role = self.add_role("Forum Mod", 3, discord.Permissions(manage_threads=True, manage_guild=True).value)
        self.forum_mod = self.add_member("forum mod", self.forum_mod_role["id"], self.role_member["id"])
        self._signed_in: dict[int, TestClient] = {}

    async def asyncTearDown(self) -> None:
        for client in self._signed_in.values():
            await client.close()
        await super().asyncTearDown()

    async def call(self, method: str, tail: str, body: Any = None, *, user_id: int | None = None) -> tuple[int, Any]:
        """The dashboard as one person who can open every page."""
        user_id = user_id or self.admin_id
        if user_id not in self._signed_in:
            self.bot.ops.access_for = lambda guild_id, who: "full"
            app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=User(user_id, "someone", None))
            client = TestClient(TestServer(app))
            await client.start_server()
            self._signed_in[user_id] = client
        answer = await self._signed_in[user_id].request(method, f"/api/g/{self.guild_id}{tail}", json=body)
        return answer.status, await answer.json()

    def row(self, **fields: Any) -> dict:
        return {"forum_id": str(self.forum_id), "message": "Hi {{OP.Mention}}", "inactivity_message": "", "solved_tag": str(self.tags["Solved"]), "helper_roles": [], **fields}

    async def test_the_page_gets_each_forum_with_its_tags_and_counts(self):
        await self.configure()
        await self.db.run("INSERT INTO forum_reminders (guild_id, thread_id, channel_id, reminded_at) VALUES (?, 1, ?, 0), (?, 2, ?, 0)", (self.guild_id, self.forum_id, self.guild_id, self.forum_id))
        status, view = await self.call("GET", "/forummessages/forums")
        self.assertEqual(status, 200, view)
        [forum] = view["forums"]
        self.assertEqual((forum["id"], forum["name"], forum["type"]), (str(self.forum_id), "reviews", 15))
        self.assertEqual([(tag["name"], tag["moderated"]) for tag in forum["tags"]], [("Solved", False), ("Question", False), ("Problem", False), ("Other", True)])
        self.assertEqual(view["counts"], [{"forum_id": str(self.forum_id), "welcomed": 0, "reminded": 2}])
        self.assertEqual(view["default_reminder"], fm.DEFAULT_REMINDER)

    async def test_a_save_takes_a_forum_and_keeps_its_text_exactly(self):
        text = "Hey {{OP.Mention}}\n\n**Read the rules.**  "
        status, saved = await self.call("PUT", "/settings/forummessages", {"forums": [self.row(message=text)], "log_channel": self.log_channel["id"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.settings()["forums"][0]["message"], text)
        self.assertEqual(self.settings()["log_channel"], self.log_id)

    async def test_everyone_cant_be_a_helper_role_even_for_the_owner(self):
        status, answer = await self.call("PUT", "/settings/forummessages", {"forums": [self.row(helper_roles=[str(self.guild_id)])]}, user_id=self.owner_id)
        self.assertEqual(status, 400)
        self.assertEqual(answer["fields"]["forums"], access.NOT_EVERYONE)

    async def test_a_ping_the_person_couldnt_send_is_refused_and_an_old_one_stays(self):
        status, answer = await self.call("PUT", "/settings/forummessages", {"forums": [self.row(message="@everyone new post")]}, user_id=self.forum_mod)
        self.assertEqual(status, 400, answer)
        self.assertEqual(answer["error"], "You need Mention Everyone in that channel to ping through the bot.")
        await self.configure(message="<@&%s> look" % self.role_mod["id"])  # set up by someone who could ping that role
        status, answer = await self.call("PUT", "/settings/forummessages", {"forums": [self.row(message="<@&%s> look, please" % self.role_mod["id"], solved_tag=None)]}, user_id=self.forum_mod)
        self.assertEqual(status, 200, answer)

    async def test_a_ping_in_the_welcome_doesnt_open_the_reminder_to_it(self):
        """Review finding 4, on the page: the reminder can't take a ping the person couldn't send because the welcome already holds it."""
        staff = f"<@&{self.role_mod['id']}>"
        welcome = f"Hey {{{{OP.Mention}}}}, welcome. {staff} please look."
        await self.configure(message=welcome, inactivity_message="Any news?")
        status, answer = await self.call("PUT", "/settings/forummessages", {"forums": [self.row(message=welcome, inactivity_message=f"Still stuck? {staff} will look.",
                                                                                             helper_roles=[str(self.helper_id)])]}, user_id=self.forum_mod)
        self.assertEqual(status, 400, answer)
        self.assertEqual(answer["error"], "You need Mention Everyone in that channel to ping through the bot.")

    async def test_a_forum_the_person_cant_post_in_is_refused(self):
        quiet = {**self.forum, "permission_overwrites": [{"id": str(self.guild_id), "type": 0, "allow": "0", "deny": str(discord.Permissions(send_messages=True).value)}]}
        self.state.parse_channel_update(quiet)
        status, answer = await self.call("PUT", "/settings/forummessages", {"forums": [self.row()]}, user_id=self.forum_mod)
        self.assertEqual(status, 400, answer)
        self.assertEqual(answer["fields"]["forums"], "You can't post in that channel.")
