"""
The dashboard's HTTP server, running inside the bot process (aiohttp) so
every action goes through the same live bot, data and Discord connection
the slash commands use - one source of truth, no second process writing
the same files.

Routes:
  /auth/login, /auth/callback, /auth/logout   Discord login
  /api/...                                    JSON API (login required)
  /api/ws                                     live updates (WebSocket)
  everything else                             the built frontend (web/dist)

Binds to 127.0.0.1 by default: put a reverse proxy (Caddy) in front of it
for real use, or reach it over an SSH tunnel while testing.
"""

import asyncio
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import quote

import discord
from aiohttp import WSMsgType, web

import config
import db
import secret_store
from webapi import auth, integrations
from webapi import settings as settings_api

log = logging.getLogger("wow-apply-bot.web")
audit_logger = logging.getLogger("wow-apply-bot.audit")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "web", "dist")
CSRF_HEADER = "X-Crusader-Request"

BOT_KEY = web.AppKey("bot", object)
HUB_KEY = web.AppKey("hub", object)
STARTED_KEY = web.AppKey("started", float)


def _git_version() -> str:
    # Docker images have no .git folder: the build passes the commit in.
    if os.environ.get("APP_VERSION"):
        return os.environ["APP_VERSION"][:12]
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


VERSION = _git_version()


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, default=str))


def _error(message, status):
    return _json({"error": message}, status=status)


# --- live updates ----------------------------------------------------------------

class Hub:
    """Fan-out of events to every open dashboard tab."""

    def __init__(self):
        self.sockets = set()

    async def broadcast(self, event: dict):
        payload = json.dumps(event, default=str)
        for ws in list(self.sockets):
            try:
                await ws.send_str(payload)
            except Exception:
                self.sockets.discard(ws)


# --- audit -------------------------------------------------------------------------

def audit(request, action: str):
    """Same tab-separated line format cogs/audit_log.py writes, so web
    actions show up in /auditlog (and the future Audit Log page) next to
    Discord ones. Written only if the audit log feature is loaded."""
    user = request["user"]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    audit_logger.info("%s\t%s (%s)\tweb dashboard\tWEB %s", timestamp, user["username"], user["user_id"], action)


# --- middlewares ---------------------------------------------------------------------

@web.middleware
async def security_headers(request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@web.middleware
async def require_login(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)

    session = auth.get_session(request.cookies.get(auth.SESSION_COOKIE))
    if session is None:
        return _error("Not logged in", 401)
    bot = request.app[BOT_KEY]
    levels = await auth.levels_for(bot, session["user_id"])
    if not auth.can_access(levels):
        return _error("Your Discord roles don't give dashboard access", 403)

    # CSRF: browsers won't send a custom header cross-site without a CORS
    # preflight (which this server never approves), and SameSite=Lax
    # cookies aren't sent on cross-site POSTs in the first place.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        return _error("Missing request header", 403)

    request["user"] = session
    request["levels"] = levels
    return await handler(request)


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Dashboard request failed: %s %s", request.method, request.path)
        if request.path.startswith("/api/"):
            return _error("Something went wrong on the bot's side - check the bot console", 500)
        raise


# --- auth routes -------------------------------------------------------------------

async def login(request):
    bot = request.app[BOT_KEY]
    if not auth.client_secret():
        return web.Response(text="DISCORD_CLIENT_SECRET isn't set in .env - see the README's Web dashboard section.",
                            status=500)
    state = secrets.token_urlsafe(24)
    response = web.HTTPFound(auth.login_url(bot, state))
    response.set_cookie(auth.STATE_COOKIE, state, max_age=600, httponly=True,
                        secure=auth.cookie_secure(), samesite="Lax", path="/auth")
    return response


def _login_error(message):
    return web.HTTPFound(f"/login?error={quote(message)}")


async def callback(request):
    bot = request.app[BOT_KEY]
    if request.query.get("error"):
        return _login_error("Login was cancelled.")
    state = request.query.get("state")
    if not state or state != request.cookies.get(auth.STATE_COOKIE):
        return _login_error("Login expired - please try again.")
    try:
        user = await auth.exchange_code(bot, request.query.get("code", ""))
    except PermissionError as exc:
        return _login_error(str(exc))

    levels = await auth.levels_for(bot, int(user["id"]), fresh=True)
    if not auth.can_access(levels):
        log.info("Dashboard login refused for %s (%s): no moderator role", user.get("username"), user["id"])
        return _login_error("Your Discord account doesn't have the moderator role in the server.")

    token = auth.create_session(user)
    auth.purge_expired_sessions()
    log.info("Dashboard login: %s (%s)", user.get("username"), user["id"])
    response = web.HTTPFound("/")
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        secure=auth.cookie_secure(), samesite="Lax", path="/")
    response.del_cookie(auth.STATE_COOKIE, path="/auth")
    return response


async def logout(request):
    if request.headers.get(CSRF_HEADER) != "1":
        return _error("Missing request header", 403)
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    response = _json({"ok": True})
    response.del_cookie(auth.SESSION_COOKIE, path="/")
    return response


# --- API ---------------------------------------------------------------------------

async def me(request):
    user = request["user"]
    return _json({
        "id": str(user["user_id"]),
        "username": user["username"],
        "avatar_url": auth.avatar_url(user["user_id"], user["avatar"]),
        "levels": sorted(request["levels"]),
    })


def _status(app) -> dict:
    bot = app[BOT_KEY]
    guild = auth.guild_for(bot)
    pending_applications = sum(
        1 for _key, record in bot.store.items()
        if isinstance(record, dict) and record.get("applicant_id") and record.get("status") == "pending"
    )
    warnings = settings_api.dependency_warnings()
    pending_restart = settings_api.pending_restart_keys()
    if os.environ.get("STORAGE_BACKEND", "json").lower() != "sqlite":
        warnings.append("Data is still stored in JSON files (STORAGE_BACKEND isn't set to sqlite).")
    latency = bot.latency
    return {
        "online": bot.is_ready() and not bot.is_closed(),
        "latency_ms": round(latency * 1000) if latency == latency and latency != float("inf") else None,
        "uptime_seconds": round(time.monotonic() - app[STARTED_KEY]),
        "version": VERSION,
        # e.g. "test" - shown as a badge so a test dashboard is never mistaken for live.
        "environment": os.environ.get("WEB_ENVIRONMENT") or None,
        "bot": {
            "name": str(bot.user) if bot.user else None,
            "avatar_url": bot.user.display_avatar.url if bot.user else None,
        },
        "guild": {
            "id": str(guild.id),
            "name": guild.name,
            "icon_url": guild.icon.url if guild.icon else None,
            "member_count": guild.member_count,
        } if guild else None,
        "features": {
            key: bool(getattr(config, key)) for key in settings_api.all_keys() if key.startswith("FEATURE_")
        },
        "counts": {"pending_applications": pending_applications},
        "missing_setup": settings_api.missing_setup(),
        "setup_completed": db.get_meta("setup_completed_at") is not None,
        "restart_available": _restart_available(),
        "pending_restart": pending_restart,
        "warnings": warnings,
    }


async def status(request):
    return _json(_status(request.app))


async def guild_channels(request):
    guild = auth.guild_for(request.app[BOT_KEY])
    if guild is None:
        return _json([])
    me_member = guild.me
    result = []
    for channel in sorted(guild.channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
        if isinstance(channel, discord.CategoryChannel):
            continue
        perms = channel.permissions_for(me_member) if me_member else None
        result.append({
            "id": str(channel.id),
            "name": channel.name,
            "type": channel.type.name,
            "category": channel.category.name if channel.category else None,
            "bot_can_view": bool(perms and perms.view_channel),
            "bot_can_send": bool(perms and perms.send_messages),
        })
    return _json(result)


async def guild_roles(request):
    guild = auth.guild_for(request.app[BOT_KEY])
    if guild is None:
        return _json([])
    return _json([
        {
            "id": str(role.id),
            "name": role.name,
            "color": f"#{role.color.value:06x}" if role.color.value else None,
            "managed": role.managed,
            "position": role.position,
        }
        for role in sorted(guild.roles, key=lambda r: -r.position)
        if not role.is_default()
    ])


async def settings_list(request):
    return _json(settings_api.list_settings())


async def settings_update(request):
    key = request.match_info["key"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("Body must be JSON", 400)
    if "value" not in body:
        return _error("Missing 'value'", 400)
    guild = auth.guild_for(request.app[BOT_KEY])
    try:
        typed = settings_api.validate(key, body["value"], guild)
    except settings_api.SettingError as exc:
        return _error(str(exc), 400)
    user = request["user"]
    result = settings_api.save(key, typed, f"{user['username']} ({user['user_id']})")
    audit(request, f"settings.update {key}")
    await request.app[HUB_KEY].broadcast({"type": "settings.changed", "key": key})
    return _json(result)


async def settings_reset(request):
    key = request.match_info["key"]
    if key not in settings_api.all_keys():
        return _error("Unknown setting", 404)
    user = request["user"]
    try:
        result = settings_api.reset(key, f"{user['username']} ({user['user_id']})")
    except settings_api.SettingError as exc:
        return _error(str(exc), 400)
    audit(request, f"settings.reset {key}")
    await request.app[HUB_KEY].broadcast({"type": "settings.changed", "key": key})
    return _json(result)


async def settings_history(request):
    key = request.match_info["key"]
    if key not in settings_api.all_keys():
        return _error("Unknown setting", 404)
    return _json(settings_api.history(key))


# --- integrations (API keys) -------------------------------------------------------

async def integrations_list(request):
    return _json(integrations.list_services())


async def integration_set(request):
    key = request.match_info["key"]
    if key not in secret_store.SECRETS:
        return _error("Unknown key", 404)
    body = await request.json()
    value = str(body.get("value", "")).strip()
    if not value:
        return _error("Enter a value, or use Remove to clear it", 400)
    user = request["user"]
    secret_store.set(key, value, f"{user['username']} ({user['user_id']})")
    integrations.apply_to_bot(request.app[BOT_KEY])
    audit(request, f"integrations.set {key}")  # never the value
    await request.app[HUB_KEY].broadcast({"type": "settings.changed", "key": key})
    return _json(secret_store.describe(key))


async def integration_delete(request):
    key = request.match_info["key"]
    if key not in secret_store.SECRETS:
        return _error("Unknown key", 404)
    secret_store.delete(key)
    integrations.apply_to_bot(request.app[BOT_KEY])
    audit(request, f"integrations.remove {key}")
    await request.app[HUB_KEY].broadcast({"type": "settings.changed", "key": key})
    return _json(secret_store.describe(key))


async def integration_test(request):
    service = request.match_info["service"]
    if service not in secret_store.SERVICES:
        return _error("Unknown service", 404)
    body = await request.json()
    id_key, secret_key = secret_store.SERVICES[service][2]
    # Blank fields = test what's already saved.
    client_id = str(body.get("client_id") or "").strip() or secret_store.get(id_key)
    client_secret = str(body.get("client_secret") or "").strip() or secret_store.get(secret_key)
    ok, message = await integrations.test(service, client_id, client_secret)
    return _json({"ok": ok, "message": message})


# --- setup & restart -----------------------------------------------------------------

def _restart_available() -> bool:
    """Only when something restarts the process after it exits (Docker's
    restart policy, a systemd Restart=, AMP's auto-restart) - otherwise a
    restart from the dashboard would just stop the bot."""
    return os.environ.get("WEB_RESTART_ENABLED", "").strip().lower() in ("1", "true", "yes")


async def setup_state(request):
    bot = request.app[BOT_KEY]
    guild = auth.guild_for(bot)
    checks = []
    if guild is not None and guild.me is not None:
        perms = guild.me.guild_permissions
        for attr, label in [
            ("view_channel", "View channels"),
            ("send_messages", "Send messages"),
            ("embed_links", "Embed links"),
            ("read_message_history", "Read message history"),
            ("add_reactions", "Add reactions"),
            ("manage_messages", "Manage messages (pin/unpin, clean up)"),
            ("manage_roles", "Manage roles (give approved applicants their roles)"),
            ("manage_nicknames", "Manage nicknames (rename approved applicants)"),
            ("create_public_threads", "Create posts in the raid-summary forum"),
        ]:
            checks.append({"label": label, "ok": bool(getattr(perms, attr, False))})
        fresh = guild.get_role(config.FRESH_ROLE_ID) if config.FRESH_ROLE_ID else None
        if fresh is not None:
            checks.append({
                "label": f"Bot's role is above @{fresh.name} (needed to hand it out)",
                "ok": guild.me.top_role > fresh,
            })
    return _json({
        "completed": db.get_meta("setup_completed_at") is not None,
        "missing": settings_api.missing_setup(),
        "checks": checks,
        "guild": {"name": guild.name, "icon_url": guild.icon.url if guild.icon else None} if guild else None,
        "restart_available": _restart_available(),
    })


async def setup_complete(request):
    db.set_meta("setup_completed_at", datetime.now(timezone.utc).isoformat())
    audit(request, "setup.complete")
    return _json({"ok": True})


async def restart(request):
    if "admin" not in request["levels"]:
        return _error("Only admins can restart the bot", 403)
    if not _restart_available():
        return _error("Restarting from the dashboard isn't enabled on this server (WEB_RESTART_ENABLED)", 400)
    audit(request, "bot.restart")
    log.warning("Restart requested from the dashboard by %s", request["user"]["username"])
    bot = request.app[BOT_KEY]

    async def _shutdown():
        await asyncio.sleep(0.5)  # let this response reach the browser first
        await bot.close()  # bot.py's main() then returns and the process exits

    asyncio.get_running_loop().create_task(_shutdown())
    return _json({"ok": True}, status=202)


async def websocket(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    hub = request.app[HUB_KEY]
    hub.sockets.add(ws)
    try:
        await ws.send_str(json.dumps({"type": "status", "data": _status(request.app)}, default=str))
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.sockets.discard(ws)
    return ws


async def _status_pusher(app):
    """Pushes bot status to open dashboards every 15 s (latency, uptime)."""
    while True:
        await asyncio.sleep(15)
        hub = app[HUB_KEY]
        if hub.sockets:
            try:
                await hub.broadcast({"type": "status", "data": _status(app)})
            except Exception:
                log.exception("Status push failed")


# --- frontend ------------------------------------------------------------------------

async def frontend(request):
    """Serves web/dist. Unknown paths get index.html (client-side routing)."""
    if not os.path.isdir(DIST):
        return web.Response(
            text="The dashboard frontend isn't built yet: run `npm install && npm run build` in the web/ folder.",
            status=503,
        )
    relative = request.match_info.get("path", "")
    candidate = os.path.normpath(os.path.join(DIST, relative))
    if relative and candidate.startswith(DIST + os.sep) and os.path.isfile(candidate):
        response = web.FileResponse(candidate)
        if "/assets/" in candidate:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
    response = web.FileResponse(os.path.join(DIST, "index.html"))
    response.headers["Cache-Control"] = "no-cache"
    return response


def create_app(bot) -> web.Application:
    app = web.Application(middlewares=[errors, security_headers, require_login], client_max_size=10 * 1024 * 1024)
    app[BOT_KEY] = bot
    app[HUB_KEY] = Hub()
    app[STARTED_KEY] = time.monotonic()

    app.router.add_get("/auth/login", login)
    app.router.add_get("/auth/callback", callback)
    app.router.add_post("/auth/logout", logout)

    app.router.add_get("/api/me", me)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/guild/channels", guild_channels)
    app.router.add_get("/api/guild/roles", guild_roles)
    app.router.add_get("/api/settings", settings_list)
    app.router.add_put("/api/settings/{key}", settings_update)
    app.router.add_delete("/api/settings/{key}", settings_reset)
    app.router.add_get("/api/settings/{key}/history", settings_history)
    app.router.add_get("/api/integrations", integrations_list)
    app.router.add_put("/api/integrations/{key}", integration_set)
    app.router.add_delete("/api/integrations/{key}", integration_delete)
    app.router.add_post("/api/integrations/{service}/test", integration_test)
    app.router.add_get("/api/setup", setup_state)
    app.router.add_post("/api/setup/complete", setup_complete)
    app.router.add_post("/api/bot/restart", restart)
    app.router.add_get("/api/ws", websocket)
    app.router.add_route("*", "/api/{tail:.*}", lambda r: _error("Not found", 404))

    app.router.add_get("/{path:.*}", frontend)

    async def start_background(app):
        task = asyncio.create_task(_status_pusher(app))
        yield
        task.cancel()

    app.cleanup_ctx.append(start_background)
    return app
