"""Shared by the report and voice tests: the real library on a faked network, with short ways to click, submit and read what was posted."""
from __future__ import annotations

import datetime
from typing import Any

import discord
from aiohttp.test_utils import TestClient, TestServer

from captainguild.core.ops import DiscordOps
from captainguild.web.api import User
from captainguild.web.app import build_app
from tests.gateway import FakeHTTPResponse, GatewayTest, iso, new_id, user_json


class Harness(GatewayTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.net.keep_history = True
        self.state = self.bot._connection
        self.bot.ops = DiscordOps(self.bot)  # the real operations, so the dashboard's edits reach the faked network too
        self._clients: list[TestClient] = []

    async def asyncTearDown(self) -> None:
        for client in self._clients:
            await client.close()
        await super().asyncTearDown()

    # ---- what happened ----

    def posts_in(self, channel_id: int | str) -> list[dict]:
        wanted = int(channel_id)
        return sorted((m for m in self.net.messages.values() if int(m["channel_id"]) == wanted), key=lambda m: int(m["id"]))

    def calls_to(self, method: str, fragment: str) -> list[tuple]:
        return [call for call in self.net.calls if call[0] == method and fragment in call[1]]

    def answers(self) -> list[dict]:
        """Everything answered to interactions so far, as the bodies sent."""
        out = []
        for method, path, body in self.net.calls:
            if path == "/interactions/{webhook_id}/{webhook_token}/callback":
                out.append(body.get("data") or {})
            elif path == "/webhooks/{webhook_id}/{webhook_token}" and method == "POST":
                out.append(body)
            elif path.startswith("/webhooks/{webhook_id}/{webhook_token}/messages/") and method == "PATCH":
                out.append(body)
        return out

    def last_answer(self) -> str:
        data = [a for a in self.answers() if a.get("content") or a.get("embeds")]
        if not data:
            return ""
        last = data[-1]
        return last.get("content") or (last.get("embeds") or [{}])[0].get("description") or ""

    def is_private(self, answer: dict) -> bool:
        return bool((answer.get("flags") or 0) & 64)

    # ---- who ----

    def add_member(self, name: str, *role_ids: str | int, joined: datetime.datetime | None = None, bot: bool = False, user_id: int | None = None) -> int:
        user_id = user_id or new_id()
        data = {
            "user": user_json(user_id, name, bot=bot), "roles": [str(r) for r in role_ids], "joined_at": iso(joined or datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)),
            "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "premium_since": None, "avatar": None, "communication_disabled_until": None,
        }
        self.member_json[user_id] = data
        self.net.members[user_id] = data
        self.state.parse_guild_member_add({"guild_id": str(self.guild_id), **data})
        return user_id

    def add_role(self, name: str, position: int, permissions: int = 0) -> dict:
        role = {"id": str(new_id()), "name": name, "color": 0, "hoist": False, "position": position, "permissions": str(permissions), "managed": False, "mentionable": False, "icon": None, "unicode_emoji": None, "flags": 0}
        self.net.roles[int(role["id"])] = role
        self.state.parse_guild_role_create({"guild_id": str(self.guild_id), "role": role})
        return role

    # ---- talking to the bot ----

    def _option_data(self, options: dict, resolved: dict) -> list[dict]:
        out = []
        for name, value in options.items():
            if isinstance(value, discord.Member):
                member = self.member_json[value.id]
                resolved.setdefault("users", {})[str(value.id)] = member["user"]
                resolved.setdefault("members", {})[str(value.id)] = {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}
                out.append({"name": name, "type": 6, "value": str(value.id)})
            elif isinstance(value, bool):
                out.append({"name": name, "type": 5, "value": value})
            elif isinstance(value, int):
                out.append({"name": name, "type": 4, "value": value})
            elif isinstance(value, discord.Role):
                resolved.setdefault("roles", {})[str(value.id)] = self.net.roles[value.id]
                out.append({"name": name, "type": 8, "value": str(value.id)})
            elif isinstance(value, dict) and value.get("filename"):
                attachment = {"id": str(new_id()), "size": 10, "url": f"https://cdn.example/{value['filename']}", "proxy_url": f"https://cdn.example/{value['filename']}", **value}
                resolved.setdefault("attachments", {})[attachment["id"]] = attachment
                out.append({"name": name, "type": 11, "value": attachment["id"]})
            else:
                out.append({"name": name, "type": 3, "value": str(value)})
        return out

    async def slash(self, name: str, user_id: int, *path: str, channel: dict | None = None, **options: Any) -> None:
        resolved: dict = {}
        data = self._option_data(options, resolved)
        for index, part in enumerate(reversed(path)):
            data = [{"name": part, "type": 1 if index == 0 else 2, "options": data}]
        payload = {"id": str(new_id()), "name": name, "type": 1, "options": data, "resolved": resolved}
        self.state.parse_interaction_create(self.interaction_payload(2, payload, user_id=user_id, channel=channel))
        await self.settle()

    async def user_menu(self, name: str, user_id: int, target_id: int) -> None:
        member = self.member_json[target_id]
        data = {
            "id": str(new_id()), "name": name, "type": 2, "target_id": str(target_id),
            "resolved": {"users": {str(target_id): member["user"]}, "members": {str(target_id): {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}}},
        }
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))
        await self.settle()

    async def message_menu(self, name: str, user_id: int, message: dict) -> None:
        data = {"id": str(new_id()), "name": name, "type": 3, "target_id": message["id"], "resolved": {"messages": {message["id"]: message}}}
        self.state.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))
        await self.settle()

    async def submit_form(self, user_id: int, *values: str, channel: dict | None = None, selects: dict[int, str] | None = None) -> None:
        """Fill the last form opened, one value for each text box, in order. `selects` picks a menu's value by its row number."""
        modal = self.net.modals[-1]
        rows = []
        boxes = iter(values)
        for index, row in enumerate(modal["components"]):
            field = row.get("component") or row["components"][0]
            if field["type"] == 4:
                filled: dict = {"type": 4, "custom_id": field["custom_id"], "value": next(boxes)}
            else:
                filled = {"type": field["type"], "custom_id": field["custom_id"], "values": [(selects or {}).get(index, field["options"][0]["value"])]}
            rows.append({"type": 18, "id": row.get("id", index), "component": filled} if row.get("type") == 18 else {"type": 1, "components": [filled]})
        self.state.parse_interaction_create(self.interaction_payload(5, {"custom_id": modal["custom_id"], "components": rows, "resolved": {}}, user_id=user_id, channel=channel))
        await self.settle()

    async def press(self, custom_id: str, message: dict, user_id: int, *, channel: dict | None = None) -> None:
        self.state.parse_interaction_create(self.interaction_payload(3, {"custom_id": custom_id, "component_type": 2}, user_id=user_id, channel=channel, message=message))
        await self.settle()

    def buttons(self, message: dict) -> dict:
        """A posted message's buttons by label: its custom_id or url, and whether it's off."""
        found = {}
        for row in message.get("components") or []:
            for part in row.get("components") or []:
                found[part.get("label")] = {"custom_id": part.get("custom_id"), "url": part.get("url"), "disabled": bool(part.get("disabled"))}
        return found

    def failing(self, method: str, fragment: str, status: int = 500) -> Any:
        """Make matching calls fail until the returned function is called."""
        real = self.bot.http.request

        async def request(route: Any, **kwargs: Any) -> Any:
            if route.method == method and fragment in route.path:
                kind = {403: discord.Forbidden, 404: discord.NotFound}.get(status, discord.HTTPException)
                raise kind(FakeHTTPResponse(status), {"code": 0, "message": "refused for a test"})
            return await real(route, **kwargs)

        self.bot.http.request = request

        def restore() -> None:
            self.bot.http.request = real
        return restore

    # ---- dashboard ----

    async def client(self, *, access: str = "full") -> TestClient:
        ops = self.bot.ops
        ops.access_for = lambda guild_id, user_id: access if guild_id == self.guild_id else None
        user = User(self.admin_id, "admin", None)
        app = build_app(self.bot, session_key=b"t" * 32, public_url="", secure_cookies=False, demo_user=user)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)
        return client

    def path(self, tail: str) -> str:
        return f"/api/g/{self.guild_id}{tail}"
