"""The audit log watcher keeps every entry the gateway sends, including one discord.py can't build a target for."""
from __future__ import annotations

import discord

from tests.gateway import GatewayTest


class AuditWatcherTest(GatewayTest):
    def entry_json(self, action: discord.AuditLogAction, target: str | None) -> dict:
        return {
            "id": str(discord.utils.time_snowflake(discord.utils.utcnow())), "guild_id": str(self.guild_id), "action_type": action.value,
            "user_id": str(self.mod_id), "target_id": target, "changes": [],
        }

    def kept(self) -> list:
        return list(self.bot.audit._entries.get(self.guild_id, ()))

    async def test_a_channel_entry_with_no_target_is_kept_without_one(self):
        # discord.py builds a channel entry's target as Object(id=target_id), which fails when Discord sent no target ID.
        self.bot._connection.parse_guild_audit_log_entry_create(self.entry_json(discord.AuditLogAction.channel_update, None))
        await self.settle()
        self.assert_no_errors()
        self.assertEqual([(entry.action, entry.target_id, entry.user_id) for entry in self.kept()], [(discord.AuditLogAction.channel_update, None, self.mod_id)])

    async def test_an_entry_keeps_the_id_of_what_it_acted_on(self):
        self.bot._connection.parse_guild_audit_log_entry_create(self.entry_json(discord.AuditLogAction.channel_update, self.text["id"]))
        await self.settle()
        self.assert_no_errors()
        self.assertEqual([entry.target_id for entry in self.kept()], [int(self.text["id"])])
