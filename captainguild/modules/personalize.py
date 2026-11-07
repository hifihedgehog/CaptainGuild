"""The bot's avatar, banner and bio in this server.

Discord holds the live profile and has no way to read a bot's bio back, so the images and bio set here are also kept here, for the page's
preview. They're stored as text so a JSON backup carries them unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

from aiohttp import web

from ..core import images
from ..core.module import Module
from ..core.validate import Invalid, ValidationError, text
from ..web.api import ApiError

BIO_MAX = 190  # Discord's limit for a member's bio


class Personalize(Module):
    key = "personalize"
    category = "Config"
    migrations = [
        """
        CREATE TABLE bot_profiles (
            guild_id INTEGER PRIMARY KEY,
            avatar TEXT,
            avatar_type TEXT,
            banner TEXT,
            banner_type TEXT,
            bio TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        """,
    ]

    def api(self, router: Any) -> None:
        router.get("/personalize", self._read)
        router.post("/personalize", self._save)
        router.delete("/personalize", self._reset)
        router.get("/personalize/image/{part:avatar|banner}", self._image)

    async def _state(self, req: Any) -> dict:
        row = await req.db.one("SELECT avatar_type, banner_type, bio, updated_at FROM bot_profiles WHERE guild_id = ?", (req.guild_id,)) or {}
        version = int(row.get("updated_at") or 0)
        return {
            "avatar": f"/api/g/{req.guild_id}/personalize/image/avatar?v={version}" if row.get("avatar_type") else None,
            "banner": f"/api/g/{req.guild_id}/personalize/image/banner?v={version}" if row.get("banner_type") else None,
            "bio": row.get("bio") or "",
            "custom": bool(row.get("avatar_type") or row.get("banner_type") or row.get("bio")),
        }

    async def _read(self, req: Any) -> dict:
        return await self._state(req)

    async def _image(self, req: Any) -> web.Response:
        part = req.param("part")
        row = await req.db.one(f"SELECT {part} AS image, {part}_type AS kind FROM bot_profiles WHERE guild_id = ?", (req.guild_id,))  # part comes from the route's two choices
        # A restored backup can hold any type here, and a page or script served from this site would run as this site.
        if row is None or not row["image"] or row["kind"] not in images.MIMES.values():
            raise ApiError(404, "No image is set.")
        return web.Response(body=base64.b64decode(row["image"]), content_type=row["kind"], headers={"Cache-Control": "private, max-age=3600"})

    async def _checked_image(self, data: bytes | None, name: str) -> tuple[bytes, str] | None:
        if data is None:
            return None
        try:
            facts = await asyncio.to_thread(images.check, data)
        except Invalid as error:
            raise ValidationError({name: str(error)}) from error
        return data, facts.mime

    async def _save(self, req: Any) -> dict:
        files, fields = await images.read_form(req.request)
        avatar = await self._checked_image(files.get("avatar"), "avatar")
        banner = await self._checked_image(files.get("banner"), "banner")
        changes: dict[str, Any] = {}
        if avatar:
            changes["avatar"] = avatar[0]
        if banner:
            changes["banner"] = banner[0]
        bio = None
        if "bio" in fields:
            try:
                bio = text(BIO_MAX)(fields["bio"])
            except Invalid as error:
                raise ValidationError({"bio": str(error)}) from error
            changes["bio"] = bio
        if not changes:
            raise ApiError(400, "There's nothing to save. Pick an image or change the bio first.")
        await req.ops.set_bot_profile(req.guild_id, **changes)  # Discord goes first, so a refusal leaves the stored copy true
        await req.db.run(
            "INSERT INTO bot_profiles (guild_id, avatar, avatar_type, banner, banner_type, bio, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET avatar = COALESCE(excluded.avatar, avatar), avatar_type = COALESCE(excluded.avatar_type, avatar_type), "
            "banner = COALESCE(excluded.banner, banner), banner_type = COALESCE(excluded.banner_type, banner_type), bio = COALESCE(?, bio), updated_at = excluded.updated_at",
            (
                req.guild_id,
                base64.b64encode(avatar[0]).decode() if avatar else None, avatar[1] if avatar else None,
                base64.b64encode(banner[0]).decode() if banner else None, banner[1] if banner else None,
                bio or "", time.time(), bio,
            ),
        )
        await req.record("personalize.update", detail=", ".join(sorted(changes)))
        return await self._state(req)

    async def _reset(self, req: Any) -> dict:
        await req.ops.set_bot_profile(req.guild_id, avatar=None, banner=None, bio=None)
        await req.db.run("DELETE FROM bot_profiles WHERE guild_id = ?", (req.guild_id,))
        await req.record("personalize.reset")
        return await self._state(req)
