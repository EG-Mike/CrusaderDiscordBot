"""
Discord login for the dashboard (OAuth2 authorization-code flow, `identify`
scope only) and server-side permission checks.

Who gets in is decided by the person's CURRENT roles in the guild, checked
on every request (cached for PERMISSION_CACHE_SECONDS), so taking a role
away in Discord also takes away dashboard access:

  - moderator: MOD_ROLE_ID or the Manage Roles permission (same rule as the
    bot's own moderator commands). Required to use the dashboard at all.
  - approver:  APPROVER_ROLE_ID (Approve/Deny/Reset applications).
  - admin:     the server owner, or a user ID listed in WEB_ADMIN_USER_IDS.
               Admins always get in, even without the moderator role.
"""

import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp
import discord

import config
import db

DISCORD_API = "https://discord.com/api/v10"
SESSION_COOKIE = "crusader_session"
STATE_COOKIE = "crusader_oauth_state"
SESSION_DAYS = 7
PERMISSION_CACHE_SECONDS = 60


def base_url() -> str:
    return os.environ.get("WEB_BASE_URL", "http://localhost:8081").rstrip("/")


def redirect_uri() -> str:
    return f"{base_url()}/auth/callback"


def cookie_secure() -> bool:
    return base_url().startswith("https://")


def admin_user_ids() -> set:
    raw = os.environ.get("WEB_ADMIN_USER_IDS", "")
    return {int(part) for part in raw.replace(";", ",").split(",") if part.strip().isdigit()}


def client_id(bot) -> str:
    return os.environ.get("DISCORD_CLIENT_ID") or str(bot.application_id)


def client_secret() -> str:
    return os.environ.get("DISCORD_CLIENT_SECRET", "")


def login_url(bot, state: str) -> str:
    params = {
        "client_id": client_id(bot),
        "response_type": "code",
        "scope": "identify",
        "redirect_uri": redirect_uri(),
        "state": state,
        "prompt": "none",
    }
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


async def exchange_code(bot, code: str) -> dict:
    """Authorization code -> the Discord user ({id, username, global_name, avatar})."""
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
            },
            auth=aiohttp.BasicAuth(client_id(bot), client_secret()),
        ) as resp:
            if resp.status != 200:
                raise PermissionError(f"Discord rejected the login (token exchange HTTP {resp.status})")
            token = (await resp.json())["access_token"]
        async with http.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status != 200:
                raise PermissionError(f"Couldn't read your Discord profile (HTTP {resp.status})")
            return await resp.json()


# --- guild / permissions -------------------------------------------------------

def guild_for(bot):
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if guild_id:
        return bot.get_guild(int(guild_id))
    return bot.guilds[0] if bot.guilds else None


_permission_cache = {}  # user_id -> (expires_monotonic, levels)


async def levels_for(bot, user_id: int, *, fresh: bool = False) -> set:
    cached = _permission_cache.get(user_id)
    if cached and not fresh and cached[0] > time.monotonic():
        return cached[1]

    levels = set()
    if user_id in admin_user_ids():
        levels.add("admin")
    guild = guild_for(bot)
    member = None
    if guild is not None:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException):
                member = None
    if member is not None:
        role_ids = {role.id for role in member.roles}
        mod_role_id = int(os.environ.get("MOD_ROLE_ID", "0") or 0)
        if member.guild_permissions.manage_roles or mod_role_id in role_ids:
            levels.add("moderator")
        if config.APPROVER_ROLE_ID in role_ids:
            levels.add("approver")
        if guild.owner_id == user_id:
            levels.add("admin")
    if "admin" in levels:
        levels.add("moderator")

    _permission_cache[user_id] = (time.monotonic() + PERMISSION_CACHE_SECONDS, levels)
    return levels


def can_access(levels: set) -> bool:
    return "moderator" in levels


# --- sessions --------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.execute(
        "INSERT INTO web_sessions (token_hash, user_id, username, avatar, expires_at) VALUES (?, ?, ?, ?, ?)",
        (
            _hash(token),
            str(user["id"]),
            user.get("global_name") or user["username"],
            user.get("avatar"),
            expires.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return token


def get_session(token: str):
    if not token:
        return None
    rows = db.query(
        "SELECT user_id, username, avatar FROM web_sessions "
        "WHERE token_hash = ? AND expires_at > datetime('now')",
        (_hash(token),),
    )
    if not rows:
        return None
    row = rows[0]
    return {"user_id": int(row["user_id"]), "username": row["username"], "avatar": row["avatar"]}


def delete_session(token: str):
    if token:
        db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_hash(token),))


def purge_expired_sessions():
    db.execute("DELETE FROM web_sessions WHERE expires_at <= datetime('now')")


def avatar_url(user_id: int, avatar) -> str:
    if avatar:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"
