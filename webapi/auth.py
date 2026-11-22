"""
Discord login for the dashboard (OAuth2 authorization-code flow, `identify`
scope only) and server-side permission checks.

Who gets in is decided by the person's CURRENT roles in the guild, checked
on every request (cached for PERMISSION_CACHE_SECONDS), so taking a role
away in Discord also takes away dashboard access. The levels (what each
one may open is in webapi/access.py):

  - admin:     the server owner, a user ID listed in WEB_ADMIN_USER_IDS
               (.env; always, so nobody can lock themselves out), or one of
               the admin roles (WEB_ACCESS_ADMIN_ROLE_IDS).
  - moderator: MOD_ROLE_ID or the Manage Roles permission (the same rule as
               the bot's own moderator commands, permissions.is_moderator).
  - reviewer:  APPROVER_ROLE_ID (Approve/Deny/Reset applications).
  - viewer:    a viewer role (WEB_ACCESS_VIEWER_ROLE_IDS), only while
               WEB_ACCESS_VIEWER_ENABLED is on.

Higher levels include the lower ones (access.implied). Without any level,
the login is refused.
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
import permissions
from webapi import access
from http_utils import basic_auth_header

DISCORD_API = "https://discord.com/api/v10"
# Browsers share cookies between ports on the same host, so the test and
# live dashboards (localhost:8081 and :8082 over a tunnel) would log each
# other out. The environment's name (WEB_ENVIRONMENT, e.g. "test") keeps
# their cookies apart; live has none and keeps the plain names.
_COOKIE_SUFFIX = "".join(c for c in os.environ.get("WEB_ENVIRONMENT", "").lower() if c.isalnum())[:20]
SESSION_COOKIE = "crusader_session" + (f"_{_COOKIE_SUFFIX}" if _COOKIE_SUFFIX else "")
STATE_COOKIE = "crusader_oauth_state" + (f"_{_COOKIE_SUFFIX}" if _COOKIE_SUFFIX else "")
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
    async with aiohttp.ClientSession(trust_env=True) as http:
        async with http.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
            },
            headers={"Authorization": basic_auth_header(client_id(bot), client_secret())},
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


def _role_ids(value) -> set:
    """A role-ID setting (dict of role ID -> name, or a list) -> its IDs."""
    return {int(key) for key in (value or ()) if str(key).strip().isdigit()}


def levels_from_member(member, guild_owner_id=None) -> set:
    """The levels a guild member's roles give (without WEB_ADMIN_USER_IDS)."""
    levels = set()
    role_ids = {role.id for role in member.roles}
    if guild_owner_id == member.id or role_ids & _role_ids(config.WEB_ACCESS_ADMIN_ROLE_IDS):
        levels.add(access.ADMIN)
    if permissions.is_moderator(member):
        levels.add(access.MODERATOR)
    if config.APPROVER_ROLE_ID and int(config.APPROVER_ROLE_ID) in role_ids:
        levels.add(access.REVIEWER)
    if config.WEB_ACCESS_VIEWER_ENABLED and role_ids & _role_ids(config.WEB_ACCESS_VIEWER_ROLE_IDS):
        levels.add(access.VIEWER)
    return levels


async def levels_for(bot, user_id: int, *, fresh: bool = False) -> set:
    cached = _permission_cache.get(user_id)
    if cached and not fresh and cached[0] > time.monotonic():
        return cached[1]

    levels = set()
    if user_id in admin_user_ids():
        levels.add(access.ADMIN)
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
        levels |= levels_from_member(member, guild.owner_id)
    levels = access.implied(levels)

    _permission_cache[user_id] = (time.monotonic() + PERMISSION_CACHE_SECONDS, levels)
    return levels


def forget_levels():
    """Drops the cached levels, e.g. after the access settings changed."""
    _permission_cache.clear()


def can_access(levels: set) -> bool:
    return access.allows(levels, access.ANY)


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


def delete_other_sessions(keep_token: str) -> int:
    """Logs out every dashboard session except the given one."""
    return db.execute("DELETE FROM web_sessions WHERE token_hash != ?", (_hash(keep_token or ""),)).rowcount


def count_sessions() -> int:
    return db.query("SELECT COUNT(*) AS n FROM web_sessions WHERE expires_at > datetime('now')")[0]["n"]


def purge_expired_sessions():
    db.execute("DELETE FROM web_sessions WHERE expires_at <= datetime('now')")


def avatar_url(user_id: int, avatar) -> str:
    if avatar:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"
