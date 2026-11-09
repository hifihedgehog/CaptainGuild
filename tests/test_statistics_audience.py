"""Whether everyone reading a reply can see a channel the reply names, on a server built with the library's own objects and shaped like the
largest one the bot serves: 43,600 members holding about 16,800 different mixes of roles, member channels behind a rules role, staff rooms,
a role that keeps some people out of one channel, and members with overwrites of their own.

The check reads roles and overwrites and never the member list, so it costs the same at any size and keeps nothing between calls.
"""
from __future__ import annotations

import gc
import random
import time
import tracemalloc
import unittest
from unittest import mock

import discord

from captainguild.core.stats import views

GUILD = 1_100_000_000_000_000_001
VIEW = discord.Permissions(view_channel=True).value
MEMBERS = 43_600


def _role(role_id: int, name: str, position: int, permissions: int = 0, *, bot_id: int | None = None) -> dict:
    data = {"id": str(role_id), "name": name, "color": 0, "hoist": False, "position": position, "permissions": str(permissions), "managed": bot_id is not None,
            "mentionable": False, "icon": None, "unicode_emoji": None, "flags": 0}
    if bot_id is not None:
        data["tags"] = {"bot_id": str(bot_id)}
    return data


def build(members: int = MEMBERS, seed: int = 11):
    """The server, and its channels by kind. Roles are spread unevenly, the way self-assigned roles are."""
    rng = random.Random(seed)
    ids = iter(range(1_200_000_000_000_000_000, 2 ** 62))
    bot_id = next(ids)
    everyone = _role(GUILD, "@everyone", 0, discord.Permissions(view_channel=True, read_message_history=True, send_messages=True).value)
    agreed = _role(next(ids), "Agreed To Rules", 1)
    staff = _role(next(ids), "Staff", 200, discord.Permissions(manage_messages=True).value)
    no_media = _role(next(ids), "No Media", 150)
    other_bot = _role(next(ids), "Another Bot", 250, bot_id=next(ids))
    picks = [_role(next(ids), f"pick {number}", 2 + number) for number in range(80)]
    levels = [_role(next(ids), f"level {number}", 90 + number) for number in range(8)]
    roles = [everyone, agreed, staff, no_media, other_bot, *picks, *levels]
    weights = [1 / (rank ** 1.1) for rank in range(1, len(picks) + 1)]

    def member_json(user_id: int, name: str, held: list[str], bot: bool = False) -> dict:
        return {"user": {"id": str(user_id), "username": name, "discriminator": "0", "global_name": None, "avatar": None, "bot": bot},
                "roles": held, "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0, "pending": False, "nick": None,
                "premium_since": None, "avatar": None, "communication_disabled_until": None}

    rows = []
    for number in range(members):
        held = [agreed["id"]] if rng.random() < 0.97 else []
        held += sorted({row["id"] for row in rng.choices(picks, weights=weights, k=rng.choice((0, 0, 1, 1, 2, 2, 3, 4, 5, 6)))})
        if rng.random() < 0.3:
            held.append(rng.choice(levels)["id"])
        if rng.random() < 0.01:
            held.append(no_media["id"])
        if number < 40:
            held.append(staff["id"])
        rows.append(member_json(next(ids), f"u{number}", held))
    rows.append(member_json(bot_id, "Captain Guild", [], True))

    def channel(name: str, overwrites: list[dict], position: int) -> dict:
        return {"id": str(next(ids)), "guild_id": str(GUILD), "name": name, "type": 0, "position": position, "permission_overwrites": overwrites,
                "parent_id": None, "nsfw": False, "topic": None, "rate_limit_per_user": 0, "last_message_id": None, "flags": 0}

    def allow(role: dict) -> dict:
        return {"id": role["id"], "type": 0, "allow": str(VIEW), "deny": "0"}

    def deny(role: dict) -> dict:
        return {"id": role["id"], "type": 0, "allow": "0", "deny": str(VIEW)}

    gated = [deny(everyone), allow(agreed), allow(other_bot)]
    member_channels = [channel(f"chat-{number}", gated, number) for number in range(60)]
    staff_channels = [channel(f"staff-{number}", [deny(everyone), allow(staff)], 100 + number) for number in range(10)]
    media = channel("media", [deny(everyone), allow(agreed), deny(no_media)], 200)  # Agreed lets every holder in, No Media or not
    open_media = channel("open-media", [deny(no_media)], 201)  # open to all but No Media, which an Agreed member can hold
    kept_out = next(row["user"]["id"] for row in rows[100:] if agreed["id"] in row["roles"])  # someone who reads the chat
    quiet = channel("quiet", [{"id": kept_out, "type": 1, "allow": "0", "deny": str(VIEW)}], 202)
    guest = next(row["user"]["id"] for row in rows[200:] if staff["id"] not in row["roles"])
    invited = channel("invited", [deny(everyone), allow(staff), {"id": guest, "type": 1, "allow": str(VIEW), "deny": "0"}], 203)
    payload = {"id": str(GUILD), "name": "A large server", "icon": None, "owner_id": rows[0]["user"]["id"], "roles": roles, "members": rows,
               "channels": [*member_channels, *staff_channels, media, open_media, quiet, invited], "threads": [], "emojis": [], "stickers": [],
               "presences": [], "voice_states": [], "member_count": len(rows), "large": True, "unavailable": False, "features": [],
               "verification_level": 0, "default_message_notifications": 0, "explicit_content_filter": 0, "mfa_level": 0, "premium_tier": 0,
               "nsfw_level": 0, "preferred_locale": "en-US", "afk_timeout": 300, "system_channel_flags": 0, "stage_instances": [],
               "guild_scheduled_events": []}
    client = discord.Client(intents=discord.Intents.all())
    state = client._connection
    state.user = discord.ClientUser(state=state, data={"id": str(bot_id), "username": "Captain Guild", "discriminator": "0", "avatar": None, "bot": True})
    guild = discord.Guild(data=payload, state=state)
    found = {name: guild.get_channel(int(data["id"])) for name, data in (("media", media), ("open_media", open_media), ("quiet", quiet), ("invited", invited))}
    return guild, [guild.get_channel(int(row["id"])) for row in member_channels], [guild.get_channel(int(row["id"])) for row in staff_channels], found


class LargeServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.guild, cls.chats, cls.staff, cls.named = build()

    def test_the_model_is_the_size_it_says(self):
        self.assertEqual(len(self.guild.members), MEMBERS + 1)
        self.assertGreater(len({tuple(sorted(member._roles)) for member in self.guild.members}), 15_000)

    def test_the_answers(self):
        general, chats, staff = self.chats[0], self.chats[1:], self.staff
        self.assertTrue(all(views.all_see(self.guild, general, other) for other in chats), "every chat channel is behind the same gate")
        self.assertFalse(views.all_see(self.guild, general, staff[0]), "a staff room isn't named in a chat channel")
        self.assertFalse(views.all_see(self.guild, staff[0], general), "the Staff role alone opens the staff room and not the chat")
        self.assertTrue(views.all_see(self.guild, staff[0], staff[1]))
        self.assertTrue(views.all_see(self.guild, general, self.named["media"]), "Agreed opens media to everyone who reads the chat, No Media or not")
        self.assertFalse(views.all_see(self.guild, general, self.named["open_media"]), "an Agreed member holding No Media reads the chat and not this")
        self.assertFalse(views.all_see(self.guild, general, self.named["quiet"]), "the member kept out of quiet reads the chat")
        self.assertFalse(views.all_see(self.guild, self.named["invited"], staff[0]), "the member let into invited can't see the staff room")
        self.assertTrue(views.all_see(self.guild, staff[0], self.named["invited"]), "Staff opens invited too")

    def test_it_takes_milliseconds_and_never_reads_the_member_list(self):
        pairs = [(self.chats[0], other) for other in [*self.chats[1:], *self.staff, *self.named.values()]]
        with mock.patch.object(discord.Guild, "members", new_callable=mock.PropertyMock, side_effect=AssertionError("the check read every member")):
            started = time.perf_counter()
            rounds = 20
            for _ in range(rounds):
                for place, other in pairs:
                    views.all_see(self.guild, place, other)
            each = (time.perf_counter() - started) / (rounds * len(pairs))
        self.assertLess(each, 0.002, f"{each * 1000:.3f} ms a check")

    def test_it_keeps_nothing_between_calls(self):
        pairs = [(self.chats[0], other) for other in [*self.chats[1:], *self.staff, *self.named.values()]]
        for place, other in pairs:  # warm up anything the library caches on its own objects
            views.all_see(self.guild, place, other)
        gc.collect()
        ignored = [tracemalloc.Filter(False, tracemalloc.__file__)]
        tracemalloc.start()
        try:
            before = tracemalloc.take_snapshot().filter_traces(ignored)
            for _ in range(50):
                for place, other in pairs:
                    views.all_see(self.guild, place, other)
            gc.collect()
            after = tracemalloc.take_snapshot().filter_traces(ignored)
        finally:
            tracemalloc.stop()
        kept = sum(stat.size_diff for stat in after.compare_to(before, "filename") if stat.size_diff > 0)
        self.assertLess(kept, 64 * 1024, f"{kept:,} bytes kept after {50 * len(pairs):,} checks")


if __name__ == "__main__":
    unittest.main()
