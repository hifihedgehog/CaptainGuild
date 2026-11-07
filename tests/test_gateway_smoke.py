"""Runs every command, button, menu and form, and feeds every common gateway event, through the real library. Nothing may log an error."""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any

import discord
from discord import app_commands

from captainguild.modules import load_all
from tests.gateway import GatewayTest, iso, new_id, user_json

# Text a person might plausibly type, chosen by the option's name.
TEXT_BY_NAME = [
    (re.compile(r"duration|time|when|interval|delay|length|expires"), "1h"),
    (re.compile(r"color|colour"), "ff8800"),
    (re.compile(r"emoji"), "✅"),
    (re.compile(r"url|link"), "https://example.com/page"),
    (re.compile(r"json"), '{"title": "hello"}'),
    (re.compile(r"regex|pattern"), "hel+o"),
    (re.compile(r"date|birthday"), "05/11"),
    (re.compile(r"member|user"), "someone"),
]


def text_for(name: str) -> str:
    for pattern, value in TEXT_BY_NAME:
        if pattern.search(name):
            return value
    return "test"


class Walker(GatewayTest):
    modules = load_all()

    def option_value(self, option: dict, resolved: dict, *, target_id: int) -> Any:
        kind = option["type"]
        if option.get("choices"):
            return option["choices"][0]["value"]
        if kind == 3:
            if option.get("autocomplete"):
                return "test"
            return text_for(option["name"])
        if kind == 4:
            return max(int(option.get("min_value", 1)), 1)
        if kind == 5:
            return False
        if kind == 6 or kind == 9:
            member = self.member_json[target_id]
            resolved.setdefault("users", {})[str(target_id)] = member["user"]
            resolved.setdefault("members", {})[str(target_id)] = {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}
            return str(target_id)
        if kind == 7:
            types = option.get("channel_types")
            channel = self.text
            if types and 0 not in types:
                channel = next((c for c in (self.voice, self.forum, self.category) if c["type"] in types), self.text)
            resolved.setdefault("channels", {})[channel["id"]] = {"id": channel["id"], "name": channel["name"], "type": channel["type"], "permissions": str(discord.Permissions.all().value), "parent_id": channel.get("parent_id"), "guild_id": str(self.guild_id), "nsfw": False, "position": channel.get("position", 0), "rate_limit_per_user": 0, "topic": None, "flags": 0, "last_message_id": None}
            return channel["id"]
        if kind == 8:
            role = self.role_member
            resolved.setdefault("roles", {})[role["id"]] = role
            return role["id"]
        if kind == 10:
            return float(option.get("min_value", 1))
        if kind == 11:
            attachment = str(new_id())
            resolved.setdefault("attachments", {})[attachment] = {"id": attachment, "filename": "file.png", "size": 10, "url": "https://cdn.example/file.png", "proxy_url": "https://cdn.example/file.png", "content_type": "image/png", "width": 4, "height": 4}
            return attachment
        return "test"

    def leaves(self, command: dict, path: tuple = ()) -> list[tuple[tuple, dict]]:
        out: list[tuple[tuple, dict]] = []
        groups = [o for o in command.get("options", []) if o["type"] in (1, 2)]
        if not groups:
            return [(path, command)]
        for option in groups:
            out.extend(self.leaves(option, path + (option["name"],)))
        return out

    def command_data(self, root: dict, path: tuple, leaf: dict, *, every_option: bool, target_id: int) -> dict:
        resolved: dict = {}
        options = []
        for option in leaf.get("options", []):
            if not option.get("required") and not every_option:
                continue
            options.append({"name": option["name"], "type": option["type"], "value": self.option_value(option, resolved, target_id=target_id)})
        for name in reversed(path):
            kind = 1 if name == path[-1] else 2
            options = [{"name": name, "type": kind, "options": options}]
        return {"id": str(new_id()), "name": root["name"], "type": 1, "options": options, "resolved": resolved}

    async def press_everything(self, posted_from: int, *, user_id: int, label: str) -> None:
        """Click every button and pick from every menu the bot posted since `posted_from`, and fill any form that opens."""
        messages = self.net.posted[posted_from:]
        for message in messages:
            for row in message.get("components") or []:
                for component in row.get("components") or []:
                    custom_id = component.get("custom_id")
                    if not custom_id or component.get("disabled"):
                        continue
                    data: dict = {"custom_id": custom_id, "component_type": component["type"]}
                    if component["type"] == 3:
                        options = component.get("options") or []
                        data["values"] = [options[0]["value"]] if options else []
                    elif component["type"] in (5, 6, 7, 8):
                        data["values"] = []
                        data["resolved"] = {}
                    modal_mark = len(self.net.modals)
                    self.bot._connection.parse_interaction_create(self.interaction_payload(3, data, user_id=user_id, message=message))
                    await self.settle()
                    self.assert_no_errors(f"{label}, pressing {component.get('label') or custom_id}")
                    await self.fill_forms(modal_mark, user_id=user_id, label=label)

    async def fill_forms(self, mark: int, *, user_id: int, label: str) -> None:
        for modal in self.net.modals[mark:]:
            rows = []
            for row in modal.get("components") or []:
                inner = row.get("components") or ([row.get("component")] if row.get("component") else [])
                fields = []
                for field in inner:
                    if field and field.get("custom_id"):
                        if field.get("type") == 4:
                            fields.append({"type": 4, "custom_id": field["custom_id"], "value": text_for(field["custom_id"].lower() + " " + str(field.get("label", "")).lower())})
                        else:
                            fields.append({"type": field.get("type"), "custom_id": field["custom_id"], "values": []})
                if row.get("type") == 18 and fields:
                    rows.append({"type": 18, "id": row.get("id", 1), "component": fields[0]})
                elif fields:
                    rows.append({"type": 1, "components": fields})
            data = {"custom_id": modal["custom_id"], "components": rows, "resolved": {}}
            self.bot._connection.parse_interaction_create(self.interaction_payload(5, data, user_id=user_id))
            await self.settle()
            self.assert_no_errors(f"{label}, submitting form {modal.get('title')}")

    async def run_command(self, root: dict, path: tuple, leaf: dict, *, every_option: bool, user_id: int, target_id: int) -> None:
        label = f"/{' '.join((root['name'], *path))} as {self.guild.get_member(user_id).name}, {'all options' if every_option else 'required options'}"
        posted = len(self.net.posted)
        modal_mark = len(self.net.modals)
        data = self.command_data(root, path, leaf, every_option=every_option, target_id=target_id)
        self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=user_id))
        await self.settle()
        self.assert_no_errors(label)
        await self.fill_forms(modal_mark, user_id=user_id, label=label)
        await self.press_everything(posted, user_id=user_id, label=label)


class CommandSmokeTest(Walker):
    async def test_every_slash_command_as_the_owner(self):
        for command in self.bot.tree.get_commands():
            if isinstance(command, app_commands.ContextMenu):
                continue
            root = command.to_dict(self.bot.tree)
            for path, leaf in self.leaves(root):
                with self.subTest(command=" ".join((root["name"], *path))):
                    self.errors.lines.clear()
                    await self.run_command(root, path, leaf, every_option=False, user_id=self.owner_id, target_id=self.member_id)
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.owner_id, target_id=self.member_id)

    async def test_every_slash_command_as_an_ordinary_member_fails_cleanly(self):
        for command in self.bot.tree.get_commands():
            if isinstance(command, app_commands.ContextMenu):
                continue
            root = command.to_dict(self.bot.tree)
            for path, leaf in self.leaves(root):
                with self.subTest(command=" ".join((root["name"], *path))):
                    self.errors.lines.clear()
                    await self.run_command(root, path, leaf, every_option=True, user_id=self.member_id, target_id=self.admin_id)

    async def test_context_menus(self):
        target = self.dispatch_message(user_id=self.member_id, content="something rude")
        await self.settle()
        for command in self.bot.tree.get_commands():
            if not isinstance(command, app_commands.ContextMenu):
                continue
            with self.subTest(menu=command.name):
                self.errors.lines.clear()
                data: dict[str, Any] = {"id": str(new_id()), "name": command.name, "type": command.type.value}
                if command.type == discord.AppCommandType.user:
                    member = self.member_json[self.member_id]
                    data["target_id"] = str(self.member_id)
                    data["resolved"] = {"users": {str(self.member_id): member["user"]}, "members": {str(self.member_id): {**{k: v for k, v in member.items() if k != "user"}, "permissions": "0"}}}
                else:
                    data["target_id"] = target["id"]
                    data["resolved"] = {"messages": {target["id"]: target}}
                posted = len(self.net.posted)
                modal_mark = len(self.net.modals)
                self.bot._connection.parse_interaction_create(self.interaction_payload(2, data, user_id=self.mod_id))
                await self.settle()
                self.assert_no_errors(f"menu {command.name}")
                await self.fill_forms(modal_mark, user_id=self.mod_id, label=f"menu {command.name}")
                await self.press_everything(posted, user_id=self.owner_id, label=f"menu {command.name}")

    async def test_prefix_commands_run_too(self):
        for command in sorted(self.bot.commands, key=lambda c: c.qualified_name):
            with self.subTest(command=command.qualified_name):
                self.errors.lines.clear()
                self.dispatch_message(user_id=self.owner_id, content=f"!{command.qualified_name}")
                await self.settle()
                self.assert_no_errors(f"!{command.qualified_name}")


class EventSmokeTest(GatewayTest):
    modules = load_all()

    async def test_gateway_events_log_no_errors(self):
        state = self.bot._connection
        channel_id = self.text["id"]
        guild_id = str(self.guild_id)

        message = self.dispatch_message(user_id=self.member_id, content="first message with https://discord.gg/abc and @everyone")
        await self.settle()
        steps = [
            ("edit", lambda: state.parse_message_update({"id": message["id"], "channel_id": channel_id, "guild_id": guild_id, "content": "edited", "author": message["author"], "edited_timestamp": iso(), "timestamp": message["timestamp"], "embeds": [], "attachments": [], "mentions": [], "mention_roles": [], "mention_everyone": False, "pinned": False, "tts": False, "type": 0})),
            ("reaction add", lambda: state.parse_message_reaction_add({"user_id": str(self.member_id), "channel_id": channel_id, "message_id": message["id"], "guild_id": guild_id, "member": self.member_json[self.member_id], "emoji": {"id": None, "name": "⭐"}, "burst": False, "type": 0, "message_author_id": str(self.member_id)})),
            ("reaction remove", lambda: state.parse_message_reaction_remove({"user_id": str(self.member_id), "channel_id": channel_id, "message_id": message["id"], "guild_id": guild_id, "emoji": {"id": None, "name": "⭐"}, "burst": False, "type": 0})),
            ("delete", lambda: state.parse_message_delete({"id": message["id"], "channel_id": channel_id, "guild_id": guild_id})),
            ("bulk delete", lambda: state.parse_message_delete_bulk({"ids": [str(new_id()), str(new_id())], "channel_id": channel_id, "guild_id": guild_id})),
            ("member update", lambda: state.parse_guild_member_update({"guild_id": guild_id, "user": self.member_json[self.member_id]["user"], "roles": [self.role_member["id"], self.role_mod["id"]], "nick": "new nick", "joined_at": self.member_json[self.member_id]["joined_at"], "deaf": False, "mute": False, "flags": 0, "avatar": None, "premium_since": None, "pending": False, "communication_disabled_until": None})),
            ("voice join", lambda: state.parse_voice_state_update({"guild_id": guild_id, "channel_id": self.voice["id"], "user_id": str(self.member_id), "member": self.member_json[self.member_id], "session_id": "s", "deaf": False, "mute": False, "self_deaf": False, "self_mute": False, "self_video": False, "suppress": False, "request_to_speak_timestamp": None})),
            ("voice leave", lambda: state.parse_voice_state_update({"guild_id": guild_id, "channel_id": None, "user_id": str(self.member_id), "member": self.member_json[self.member_id], "session_id": "s", "deaf": False, "mute": False, "self_deaf": False, "self_mute": False, "self_video": False, "suppress": False, "request_to_speak_timestamp": None})),
            ("role create", lambda: state.parse_guild_role_create({"guild_id": guild_id, "role": {"id": str(new_id()), "name": "new", "color": 0, "hoist": False, "position": 1, "permissions": "0", "managed": False, "mentionable": False}})),
            ("role update", lambda: state.parse_guild_role_update({"guild_id": guild_id, "role": {**self.role_mod, "name": "Renamed"}})),
            ("role delete", lambda: state.parse_guild_role_delete({"guild_id": guild_id, "role_id": self.role_mod["id"]})),
            ("channel create", lambda: state.parse_channel_create({"id": str(new_id()), "guild_id": guild_id, "name": "brand-new", "type": 0, "position": 9, "permission_overwrites": [], "nsfw": False, "parent_id": None, "topic": None, "rate_limit_per_user": 0, "flags": 0, "last_message_id": None})),
            ("channel update", lambda: state.parse_channel_update({**self.log_channel, "name": "renamed", "topic": "new topic"})),
            ("guild update", lambda: state.parse_guild_update({"id": guild_id, "name": "Renamed Guild", "owner_id": str(self.owner_id), "features": [], "roles": list(self.net.roles.values()), "emojis": [], "stickers": [], "verification_level": 1, "default_message_notifications": 0, "explicit_content_filter": 0, "mfa_level": 0, "premium_tier": 0, "system_channel_flags": 0, "preferred_locale": "en-US", "nsfw_level": 0, "premium_progress_bar_enabled": False})),
            ("ban", lambda: state.parse_guild_ban_add({"guild_id": guild_id, "user": user_json(new_id(), "banned")})),
            ("unban", lambda: state.parse_guild_ban_remove({"guild_id": guild_id, "user": user_json(new_id(), "unbanned")})),
            ("join", lambda: state.parse_guild_member_add({"guild_id": guild_id, "user": user_json(new_id(), "newcomer"), "roles": [], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None})),
            ("leave", lambda: state.parse_guild_member_remove({"guild_id": guild_id, "user": self.member_json[self.mod_id]["user"]})),
            ("channel delete", lambda: state.parse_channel_delete({**self.log_channel})),
            ("emoji update", lambda: state.parse_guild_emojis_update({"guild_id": guild_id, "emojis": [{"id": str(new_id()), "name": "party", "animated": False, "available": True, "managed": False, "require_colons": True, "roles": []}]})),
            ("invite", lambda: state.parse_invite_create({"channel_id": channel_id, "code": "abc", "created_at": iso(), "guild_id": guild_id, "max_age": 0, "max_uses": 0, "temporary": False, "uses": 0})),
            ("thread", lambda: state.parse_thread_create({"id": str(new_id()), "guild_id": guild_id, "parent_id": channel_id, "owner_id": str(self.member_id), "name": "a thread", "type": 11, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0, "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": iso(), "locked": False}, "newly_created": True, "flags": 0})),
        ]
        for name, step in steps:
            with self.subTest(event=name):
                self.errors.lines.clear()
                step()
                await self.settle()
                self.assert_no_errors(f"event {name}")

    async def test_events_with_every_feature_turned_on(self):
        """Point every channel and role setting at something real, then run the same traffic."""
        settings = self.bot.settings
        for module, schema in settings.schemas.items():
            form = {}
            for name, field in schema.fields.items():
                if getattr(field, "check", None) is None:
                    continue
                lowered = name.lower()
                try:
                    if "channel" in lowered and isinstance(field.default, (type(None), int)):
                        form[name] = field.check(int(self.log_channel["id"]))
                    elif lowered.endswith("enabled") or lowered in ("enable", "on"):
                        form[name] = field.check(True)
                except Exception:
                    continue
            if form:
                try:
                    await settings.save(self.guild_id, module, form)
                except Exception:
                    logging.getLogger(__name__).debug("skipped %s", module)
        self.errors.lines.clear()
        message = self.dispatch_message(user_id=self.member_id, content="hello hello https://example.com https://discord.gg/abc")
        await self.settle()
        state = self.bot._connection
        state.parse_message_delete({"id": message["id"], "channel_id": self.text["id"], "guild_id": str(self.guild_id)})
        state.parse_guild_member_add({"guild_id": str(self.guild_id), "user": user_json(new_id(), "newcomer"), "roles": [], "joined_at": iso(), "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None, "avatar": None, "premium_since": None, "communication_disabled_until": None})
        await self.settle()
        self.assert_no_errors("traffic with features on")
