"""
Dashboard API for the guild's identity and the bot's Discord profile
(Settings, General, "Guild & bot").

  GET    /branding                 public: {name, logo_url} for the login page
  GET    /branding/logo            public: the guild logo image
  PUT    /api/branding/logo        upload the logo: {"data": "<base64>"} (moderators)
  DELETE /api/branding/logo        remove it (moderators)
  GET    /api/bot/profile          the bot's name, avatar and server nickname
  PUT    /api/bot/profile          change them (admins): {"username"?, "nickname"?,
                                   "avatar"?: "<base64>" | "guild_logo"}

The guild name is the GUILD_DISPLAY_NAME setting. The logo is a file next
to the database (backed up with the data folder); its type is checked from
the file's own bytes, not from what the browser claims.
"""

import base64
import binascii
import os
import time
from typing import Optional

import discord
from aiohttp import web

import config
import db
from webapi import auth
from webapi.common import BOT_KEY, BadRequest, audit, error, forbidden_unless, json_response, read_json

EDIT_LEVEL = "moderator"
PROFILE_LEVEL = "admin"
MAX_LOGO_BYTES = 1024 * 1024
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # base64 of it must fit in the 10 MB request limit
LOGO_FILE = "guild-logo"
LOGO_META = "branding_logo"  # "<content type>|<version>"

SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def image_type(data: bytes) -> Optional[str]:
    for magic, content_type in SIGNATURES:
        if data.startswith(magic):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _logo_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db.db_path())), LOGO_FILE)


def logo_info() -> Optional[tuple]:
    """(content type, version) of the stored logo, or None."""
    value = db.get_meta(LOGO_META) if db.exists() else None
    if not value or not os.path.exists(_logo_path()):
        return None
    content_type, _, version = value.partition("|")
    return content_type, version


def branding(bot) -> dict:
    guild = auth.guild_for(bot)
    info = logo_info()
    return {
        "name": (config.GUILD_DISPLAY_NAME or "").strip() or (guild.name if guild else None),
        "logo_url": f"/branding/logo?v={info[1]}" if info else (guild.icon.url if guild and guild.icon else None),
        "custom_logo": info is not None,
    }


def _decode(body: dict, limit: int) -> bytes:
    raw = body.get("data")
    if not isinstance(raw, str) or not raw:
        raise BadRequest("Choose an image.")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if len(raw) > limit * 4 // 3 + 16:
        raise BadRequest(f"The image is too big (at most {limit // (1024 * 1024)} MB).")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise BadRequest("That isn't a valid image.") from None
    if image_type(data) is None:
        raise BadRequest("Use a PNG, JPEG, GIF or WebP image.")
    return data


# --- public ---------------------------------------------------------------------------------

async def public_branding(request):
    return json_response(branding(request.app[BOT_KEY]))


async def logo(request):
    info = logo_info()
    if info is None:
        raise web.HTTPNotFound()
    with open(_logo_path(), "rb") as f:
        data = f.read()
    return web.Response(body=data, content_type=info[0], headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


# --- logo --------------------------------------------------------------------------------------

async def upload_logo(request):
    denied = forbidden_unless(request, EDIT_LEVEL, "the guild logo")
    if denied:
        return denied
    try:
        data = _decode(await read_json(request), MAX_LOGO_BYTES)
    except BadRequest as exc:
        return error(str(exc), 400)
    if len(data) > MAX_LOGO_BYTES:
        return error("The image is too big (at most 1 MB).", 400)
    path = _logo_path()
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    db.set_meta(LOGO_META, f"{image_type(data)}|{int(time.time())}")
    audit(request, "setting", "branding.logo", "Changed the guild logo", details={"bytes": len(data)})
    return json_response(branding(request.app[BOT_KEY]))


async def remove_logo(request):
    denied = forbidden_unless(request, EDIT_LEVEL, "the guild logo")
    if denied:
        return denied
    try:
        os.remove(_logo_path())
    except FileNotFoundError:
        pass
    db.execute("DELETE FROM meta WHERE key = ?", (LOGO_META,))
    audit(request, "setting", "branding.logo_removed", "Removed the guild logo")
    return json_response(branding(request.app[BOT_KEY]))


# --- the bot's Discord profile --------------------------------------------------------------------

def _profile(bot) -> dict:
    guild = auth.guild_for(bot)
    me = guild.me if guild else None
    return {
        "username": bot.user.name if bot.user else None,
        "avatar_url": bot.user.display_avatar.url if bot.user else None,
        "nickname": me.nick if me else None,
        "can_change_nickname": bool(me and me.guild_permissions.change_nickname),
        "can_edit": False,
    }


async def get_profile(request):
    denied = forbidden_unless(request, EDIT_LEVEL, "the bot's profile")
    if denied:
        return denied
    data = _profile(request.app[BOT_KEY])
    data["can_edit"] = PROFILE_LEVEL in request["levels"]
    return json_response(data)


async def update_profile(request):
    denied = forbidden_unless(request, PROFILE_LEVEL, "the bot's profile (admins only)")
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    if bot.user is None:
        return error("The bot isn't connected to Discord right now.", 503)
    try:
        body = await read_json(request)
        changes, done = {}, []
        username = body.get("username")
        if isinstance(username, str) and username.strip() and username.strip() != bot.user.name:
            username = username.strip()
            if not 2 <= len(username) <= 32:
                raise BadRequest("The bot's name must be 2 to 32 characters.")
            changes["username"] = username
            done.append("name")
        if body.get("avatar") == "guild_logo":
            if logo_info() is None:
                raise BadRequest("Upload a guild logo first.")
            with open(_logo_path(), "rb") as f:
                changes["avatar"] = f.read()
            done.append("avatar")
        elif isinstance(body.get("avatar"), str):
            changes["avatar"] = _decode({"data": body["avatar"]}, MAX_AVATAR_BYTES)
            done.append("avatar")
        nickname = body.get("nickname", ...)
    except BadRequest as exc:
        return error(str(exc), 400)

    try:
        if changes:
            await bot.user.edit(**changes)
        guild = auth.guild_for(bot)
        if nickname is not ... and guild is not None:
            nickname = (nickname or "").strip()[:32] or None
            if nickname != guild.me.nick:
                await guild.me.edit(nick=nickname)
                done.append("server nickname")
    except discord.Forbidden:
        return error("Discord didn't allow that: the bot needs the Change Nickname permission in the server.", 400)
    except discord.HTTPException as exc:
        # e.g. "You are changing your username or Discord Tag too fast" (twice an hour at most).
        return error(f"Discord refused: {exc.text or exc.status}", 400)
    if done:
        audit(request, "setting", "bot.profile", f"Changed the bot's {', '.join(done)}", details={"changed": done})
    data = _profile(bot)
    data["can_edit"] = True
    return json_response(data)


def register(app):
    app.router.add_get("/branding", public_branding)
    app.router.add_get("/branding/logo", logo)
    app.router.add_put("/api/branding/logo", upload_logo)
    app.router.add_delete("/api/branding/logo", remove_logo)
    app.router.add_get("/api/bot/profile", get_profile)
    app.router.add_put("/api/bot/profile", update_profile)
