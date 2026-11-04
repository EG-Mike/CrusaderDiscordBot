"""
The dashboard's HTTP server, running inside the bot process (aiohttp) so
every action goes through the same live bot, data and Discord connection
the slash commands use - one source of truth, no second process writing
the same files.

Routes:
  /auth/login, /auth/callback, /auth/logout   Discord login
  /api/...                                    JSON API (login required)
  /api/ws                                     live updates (WebSocket):
                                              bot status, setting changes,
                                              new audit and system log entries
  everything else                             the built frontend (web/dist)

Binds to 127.0.0.1 by default: put a reverse proxy (Caddy) in front of it
for real use, or reach it over an SSH tunnel while testing.
"""

import asyncio
import csv
import io
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import quote

import discord
from aiohttp import WSCloseCode, WSMsgType, web

import app_version
import audit as audit_store
import config
import db
import secret_store
import system_log
from webapi import (
    announcements_api, auth, bot_messages_api, common, emoji_api, game_api, integrations, maintenance_api,
    notifications_api, polls_api,
)
from webapi import settings as settings_api

log = logging.getLogger("wow-apply-bot.web")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "web", "dist")
CSRF_HEADER = common.CSRF_HEADER
BOT_KEY, HUB_KEY, STARTED_KEY = common.BOT_KEY, common.HUB_KEY, common.STARTED_KEY
Hub = common.Hub
_json, _error, audit = common.json_response, common.error, common.audit

VERSION = app_version.VERSION


async def _revalidate_sockets(app):
    """Closes live connections whose login was logged out/expired or whose
    Discord roles no longer give access - a WebSocket otherwise keeps
    streaming after the HTTP checks would start refusing that person."""
    hub = app[HUB_KEY]
    bot = app[BOT_KEY]
    for ws, info in list(hub.sockets.items()):
        session = auth.get_session(info["token"])
        levels = await auth.levels_for(bot, info["user_id"]) if session else set()
        if session is None or not auth.can_access(levels):
            hub.discard(ws)
            await ws.close(code=4403, message=b"Access ended")
        else:
            info["levels"] = set(levels)


def _avatar_for(guild, actor_id):
    if guild is None or not actor_id or not str(actor_id).isdigit():
        return None
    member = guild.get_member(int(actor_id))
    return member.display_avatar.url if member else None


def _public_event(event: dict, guild) -> dict:
    """An audit event as the browser gets it: IDs are already strings;
    adds the member's current avatar when they're still in the server."""
    return {**event, "actor_avatar_url": _avatar_for(guild, event.get("actor_id"))}


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
    name = user.get("global_name") or user.get("username")
    if not auth.can_access(levels):
        log.info("Dashboard login refused for %s (%s): no moderator role", user.get("username"), user["id"])
        audit_store.record(source="dashboard", kind="login", action="auth.refused",
                           summary="Was refused dashboard access (no moderator role)",
                           actor_id=user["id"], actor_name=name)
        return _login_error("Your Discord account doesn't have the moderator role in the server.")

    token = auth.create_session(user)
    auth.purge_expired_sessions()
    log.info("Dashboard login: %s (%s)", user.get("username"), user["id"])
    audit_store.record(source="dashboard", kind="login", action="auth.login", summary="Logged in to the dashboard",
                       actor_id=user["id"], actor_name=name)
    response = web.HTTPFound("/")
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        secure=auth.cookie_secure(), samesite="Lax", path="/")
    response.del_cookie(auth.STATE_COOKIE, path="/auth")
    return response


async def logout(request):
    if request.headers.get(CSRF_HEADER) != "1":
        return _error("Missing request header", 403)
    token = request.cookies.get(auth.SESSION_COOKIE)
    session = auth.get_session(token)
    auth.delete_session(token)
    if session:
        audit_store.record(source="dashboard", kind="login", action="auth.logout",
                           summary="Logged out of the dashboard",
                           actor_id=session["user_id"], actor_name=session["username"])
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


def _system_status() -> dict:
    """System log headline for the Overview (counts only, no log text)."""
    summary = system_log.summary()
    update = summary["last_update"]
    return {
        "errors_24h": summary["errors_24h"],
        "warnings_24h": summary["warnings_24h"],
        "crashes_7d": summary["crashes_7d"],
        "last_error_at": summary["last_error_at"],
        "last_update": {
            "at": update["created_at"],
            "previous_version": update["details"].get("previous_version"),
        } if update else None,
    }


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
        "version_info": {
            "title": app_version.INFO["title"],
            "built_at": app_version.INFO["built_at"],
            "commit_url": app_version.commit_url(VERSION),
        },
        "system": _system_status(),
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
        "feature_labels": {key: label for key, (label, _help) in settings_api.FEATURES.items()},
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
    before = settings_api.describe(key)
    result = settings_api.save(key, typed, f"{user['username']} ({user['user_id']})")
    _audit_setting(request, "settings.update", f"Changed setting {result['label']}", before, result, guild)
    await request.app[HUB_KEY].broadcast({"type": "settings.changed", "key": key})
    return _json(result)


async def settings_reset(request):
    key = request.match_info["key"]
    if key not in settings_api.all_keys():
        return _error("Unknown setting", 404)
    user = request["user"]
    before = settings_api.describe(key)
    try:
        result = settings_api.reset(key, f"{user['username']} ({user['user_id']})")
    except settings_api.SettingError as exc:
        return _error(str(exc), 400)
    _audit_setting(request, "settings.reset", f"Reset setting {result['label']} to its default", before, result,
                   auth.guild_for(request.app[BOT_KEY]))
    await request.app[HUB_KEY].broadcast({"type": "settings.changed", "key": key})
    return _json(result)


def _describe_value(kind: str, value, guild):
    """A role/channel setting's ID -> its name, for the audit trail."""
    if guild is None or value in (None, "", 0, "0") or not str(value).isdigit():
        return None
    if kind == "role":
        role = guild.get_role(int(value))
        return f"@{role.name}" if role else None
    if kind == "channel":
        channel = guild.get_channel(int(value))
        return f"#{channel.name}" if channel else None
    return None


def _audit_setting(request, action: str, summary: str, before: dict, after: dict, guild):
    """Setting changes keep their old and new value (agreed exception to
    the "no content in the audit log" rule: needed for history/revert).
    Unchanged saves aren't recorded."""
    if json.dumps(before["value"], default=str) == json.dumps(after["value"], default=str):
        return
    details = {"key": after["key"], "label": after["label"], "old": before["value"], "new": after["value"]}
    for side in ("old", "new"):
        name = _describe_value(after["kind"], details[side], guild)
        if name:
            details[f"{side}_name"] = name
    audit(request, "setting", action, summary, target=after["key"], details=details)


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
    audit(request, "integration", "integrations.set", f"Saved API key {secret_store.SECRETS[key][1]}",
          target=key)  # never the value
    await request.app[HUB_KEY].broadcast({"type": "settings.changed", "key": key})
    return _json(secret_store.describe(key))


async def integration_delete(request):
    key = request.match_info["key"]
    if key not in secret_store.SECRETS:
        return _error("Unknown key", 404)
    secret_store.delete(key)
    integrations.apply_to_bot(request.app[BOT_KEY])
    audit(request, "integration", "integrations.remove", f"Removed API key {secret_store.SECRETS[key][1]}",
          target=key)
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


# --- audit log ---------------------------------------------------------------------

AUDIT_LEVEL = "moderator"  # who may read the audit trail


def _audit_filters(request) -> dict:
    """Query string -> audit.search() filters. Raises ValueError for bad input."""
    query = request.query
    filters = {}
    source = query.get("source")
    if source:
        if source not in audit_store.SOURCES:
            raise ValueError("Unknown source")
        filters["source"] = source
    kind = query.get("kind")
    if kind:
        if kind not in audit_store.KINDS:
            raise ValueError("Unknown action type")
        filters["kind"] = kind
    for name in ("actor_id", "channel_id"):
        value = query.get(name)
        if value:
            if not value.isdigit():
                raise ValueError(f"{name} must be a Discord ID")
            filters[name] = value
    for name in ("since", "until"):
        if query.get(name):
            try:
                filters[name] = audit_store.parse_time(query[name])
            except ValueError:
                raise ValueError(f"'{name}' must be an ISO date/time") from None
    q = query.get("q", "").strip()
    if q:
        filters["q"] = q[:100]
    return filters


async def audit_list(request):
    if AUDIT_LEVEL not in request["levels"]:
        return _error("You don't have access to the audit log", 403)
    try:
        filters = _audit_filters(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    try:
        before_id = int(request.query["before_id"]) if request.query.get("before_id") else None
        limit = int(request.query.get("limit", 50))
    except ValueError:
        return _error("before_id and limit must be numbers", 400)
    events, has_more = await asyncio.to_thread(audit_store.search, filters, before_id=before_id, limit=limit)
    guild = auth.guild_for(request.app[BOT_KEY])
    return _json({"events": [_public_event(e, guild) for e in events], "has_more": has_more})


async def audit_meta(request):
    """Filter options (members, channels, action types) and headline numbers."""
    if AUDIT_LEVEL not in request["levels"]:
        return _error("You don't have access to the audit log", 403)
    facets, stats = await asyncio.gather(asyncio.to_thread(audit_store.facets), asyncio.to_thread(audit_store.stats))
    guild = auth.guild_for(request.app[BOT_KEY])
    for actor in facets["actors"]:
        actor["avatar_url"] = _avatar_for(guild, actor["id"])
    return _json({
        **facets,
        "stats": stats,
        "discord_capture_enabled": bool(config.FEATURE_AUDIT_LOG_ENABLED),
    })


EXPORT_CAP = 10000


async def audit_export(request):
    """The filtered audit trail as a CSV download (newest first)."""
    if AUDIT_LEVEL not in request["levels"]:
        return _error("You don't have access to the audit log", 403)
    try:
        filters = _audit_filters(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    events = await asyncio.to_thread(audit_store.iter_all, filters, EXPORT_CAP)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time_utc", "source", "type", "member", "member_id", "channel", "channel_id",
                     "action", "summary", "target", "details"])
    for e in events:
        writer.writerow([
            e["created_at"], e["source"], audit_store.KINDS.get(e["kind"], e["kind"]), e.get("actor_name") or "",
            e.get("actor_id") or "", e.get("channel_name") or "", e.get("channel_id") or "", e["action"],
            # A leading = + - @ would make spreadsheet apps run it as a formula.
            *(_csv_safe(v) for v in (e["summary"], e.get("target") or "",
                                     json.dumps(e["details"]) if e["details"] else "")),
        ])
    audit(request, "audit", "audit.export", f"Exported {len(events)} audit log entr{'y' if len(events) == 1 else 'ies'}",
          details={"filters": filters, "rows": len(events)})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return web.Response(
        text=out.getvalue(),
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-log-{stamp}.csv"',
                 "Cache-Control": "no-store"},
    )


def _csv_safe(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


# --- system log ----------------------------------------------------------------------

SYSTEM_EVENTS_LEVEL = "moderator"  # bot started/stopped/updated/crashed
CONSOLE_LEVEL = "admin"            # console output can include member data and internals

SYSTEM_VIEWS = {
    "": {},
    "lifecycle": {"category": "lifecycle"},
    "errors": {"levels": ["ERROR", "CRITICAL"]},
    "warnings": {"levels": ["WARNING"]},
}


def _system_filters(request) -> dict:
    query = request.query
    view = query.get("view", "")
    if view not in SYSTEM_VIEWS:
        raise ValueError("Unknown view")
    filters = dict(SYSTEM_VIEWS[view])
    if CONSOLE_LEVEL not in request["levels"]:
        filters["category"] = "lifecycle"
    if query.get("since"):
        try:
            filters["since"] = audit_store.parse_time(query["since"])
        except ValueError:
            raise ValueError("'since' must be an ISO date/time") from None
    if query.get("q", "").strip():
        filters["q"] = query["q"].strip()[:100]
    return filters


async def system_log_list(request):
    if SYSTEM_EVENTS_LEVEL not in request["levels"]:
        return _error("You don't have access to the system log", 403)
    try:
        filters = _system_filters(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    try:
        before_id = int(request.query["before_id"]) if request.query.get("before_id") else None
        limit = int(request.query.get("limit", 50))
    except ValueError:
        return _error("before_id and limit must be numbers", 400)
    entries, has_more = await asyncio.to_thread(system_log.search, filters, before_id=before_id, limit=limit)
    return _json({"entries": entries, "has_more": has_more})


async def system_log_meta(request):
    if SYSTEM_EVENTS_LEVEL not in request["levels"]:
        return _error("You don't have access to the system log", 403)
    summary = await asyncio.to_thread(system_log.summary)
    return _json({
        **summary,
        "console_visible": CONSOLE_LEVEL in request["levels"],
        "version": {**app_version.INFO, "commit_url": app_version.commit_url(VERSION)},
        "uptime_seconds": round(time.monotonic() - request.app[STARTED_KEY]),
    })


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
    audit(request, "setup", "setup.complete", "Finished the setup guide")
    return _json({"ok": True})


async def restart(request):
    if "admin" not in request["levels"]:
        return _error("Only admins can restart the bot", 403)
    if not _restart_available():
        return _error("Restarting from the dashboard isn't enabled on this server (WEB_RESTART_ENABLED)", 400)
    audit(request, "bot", "bot.restart", "Restarted the bot")
    log.warning("Restart requested from the dashboard by %s", request["user"]["username"])
    system_log.stopping(f"restarted from the dashboard by {request['user']['username']}")
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
    hub.add(ws, request["user"]["user_id"], request.cookies.get(auth.SESSION_COOKIE), request["levels"])
    try:
        await ws.send_str(json.dumps({"type": "status", "data": _status(request.app)}, default=str))
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.discard(ws)
    return ws


async def _status_pusher(app):
    """Every 15 s: re-checks who may keep their live connection, then
    pushes bot status (latency, uptime) to open dashboards."""
    while True:
        await asyncio.sleep(15)
        hub = app[HUB_KEY]
        if hub.sockets:
            try:
                await _revalidate_sockets(app)
                await hub.broadcast({"type": "status", "data": _status(app)})
            except Exception:
                log.exception("Status push failed")


async def api_not_found(request):
    return _error("Not found", 404)


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


async def _close_live_connections(app):
    """On shutdown, close every dashboard tab's live connection first: the
    server otherwise waits for them (up to a minute), which is longer than
    Docker/AMP wait before killing the bot. Tabs reconnect on their own
    once the bot is back."""
    hub = app[HUB_KEY]
    hub.closed_on_shutdown = len(hub.sockets)
    for ws in list(hub.sockets):
        hub.discard(ws)
        try:
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"Bot is restarting")
        except Exception:
            pass


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
    app.router.add_get("/api/audit", audit_list)
    app.router.add_get("/api/audit/meta", audit_meta)
    app.router.add_get("/api/audit/export", audit_export)
    app.router.add_get("/api/system-log", system_log_list)
    app.router.add_get("/api/system-log/meta", system_log_meta)
    announcements_api.register(app)
    bot_messages_api.register(app)
    emoji_api.register(app)
    notifications_api.register(app)
    maintenance_api.register(app)
    polls_api.register(app)
    game_api.register(app)
    app.router.add_get("/api/ws", websocket)
    app.router.add_route("*", "/api/{tail:.*}", api_not_found)

    app.router.add_get("/{path:.*}", frontend)

    async def start_background(app):
        task = asyncio.create_task(_status_pusher(app))
        loop = asyncio.get_running_loop()
        hub = app[HUB_KEY]

        def on_audit_event(event):
            # record() may run off the event loop thread (e.g. in to_thread).
            def send():
                guild = auth.guild_for(bot)
                loop.create_task(hub.broadcast({"type": "audit.event", "event": _public_event(event, guild)},
                                               level=AUDIT_LEVEL))
            loop.call_soon_threadsafe(send)

        def on_system_entry(entry):
            level = SYSTEM_EVENTS_LEVEL if entry["category"] == "lifecycle" else CONSOLE_LEVEL

            def send():
                loop.create_task(hub.broadcast({"type": "system.entry", "entry": entry}, level=level))
            loop.call_soon_threadsafe(send)

        unsubscribers = [audit_store.subscribe(on_audit_event), system_log.subscribe(on_system_entry)]
        yield
        for unsubscribe in unsubscribers:
            unsubscribe()
        task.cancel()

    app.cleanup_ctx.append(start_background)
    app.on_shutdown.append(_close_live_connections)
    return app
