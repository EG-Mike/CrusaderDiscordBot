"""
Dashboard API for maintenance tools.

  GET  /api/maintenance                  storage overview (moderators)
  GET  /api/maintenance/backup           download a copy of the database (admins)
  POST /api/maintenance/{action}         run a tool (admins):
       apply-retention   delete audit/system log entries past their retention, expired logins
       compact           reclaim unused space in the database file
       clear-cache       empty one API cache: {"store": "blizzard_item_cache"}
       sync-commands     re-register the slash commands in the server
       legacy-buttons    remove the old public Edit button from published announcements
       logout-others     log out every other dashboard session

Every tool is audited. Game-data tools (WarcraftLogs report cache, raid
summary diagnostics) belong to the raid features and aren't here.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timezone

import discord
from aiohttp import web

import announcements
import audit as audit_store
import config
import db
import storage
import system_log
from webapi import auth
from webapi.common import BOT_KEY, BadRequest, audit, error, forbidden_unless, json_response, read_json

VIEW_LEVEL = "moderator"
RUN_LEVEL = "admin"

# API caches that can be emptied safely: they refill on the next lookup.
CACHES = {
    "blizzard_item_cache": ("Blizzard item lookups", "Item names, quality and icons from Blizzard's API."),
    "wowhead_item_cache": ("Wowhead lookups", "Item and spell names and icons from Wowhead (fallback)."),
}

TABLES = [
    ("audit_events", "Audit log entries"),
    ("system_log", "System log entries"),
    ("announcements", "Announcements"),
    ("announcement_templates", "Announcement templates"),
    ("bot_messages", "Edited bot messages"),
    ("emoji_library", "Icon emoji"),
    ("settings", "Changed settings"),
    ("records", "Stored records (all features)"),
]


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _overview() -> dict:
    path = db.db_path()
    tables = []
    for table, label in TABLES:
        try:
            count = db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        except Exception:
            count = None
        tables.append({"table": table, "label": label, "rows": count})
    stores = [{"name": name, "records": len(store.items())} for name, store in sorted(storage.REGISTRY.items())]
    audit_file = config.AUDIT_LOG_PATH
    logs = sum(_size(p) for p in [audit_file] + [f"{audit_file}.{i}" for i in range(1, config.AUDIT_LOG_BACKUP_COUNT + 1)])
    return {
        "database": {
            "path": os.path.abspath(path),
            "size": _size(path),
            "wal_size": _size(f"{path}-wal"),
            "storage_backend": storage.backend(),
        },
        "tables": tables,
        "stores": stores,
        "audit_file_size": logs,
        "sessions": auth.count_sessions(),
        "caches": [{"store": key, "label": label, "help": help_text,
                    "records": len(storage.REGISTRY[key].items()) if key in storage.REGISTRY else None}
                   for key, (label, help_text) in CACHES.items()],
    }


async def overview(request):
    denied = forbidden_unless(request, VIEW_LEVEL, "maintenance")
    if denied:
        return denied
    data = await asyncio.to_thread(_overview)
    data["can_run"] = RUN_LEVEL in request["levels"]
    return json_response(data)


async def backup(request):
    denied = forbidden_unless(request, RUN_LEVEL, "database backups (admins only)")
    if denied:
        return denied
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    with tempfile.TemporaryDirectory() as folder:
        target = os.path.join(folder, "backup.db")
        await asyncio.to_thread(db.backup_to, target)
        with open(target, "rb") as f:
            data = f.read()
    audit(request, "maintenance", "maintenance.backup", "Downloaded a database backup", details={"bytes": len(data)})
    return web.Response(body=data, content_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="crusader-backup-{stamp}.db"', "Cache-Control": "no-store"})


async def _apply_retention(request, body) -> str:
    events = audit_store.purge()
    entries = system_log.purge()
    sessions = db.execute("DELETE FROM web_sessions WHERE expires_at <= datetime('now')").rowcount
    return f"Removed {events} old audit entries, {entries} old system log entries and {sessions} expired logins."


async def _compact(request, body) -> str:
    before = _size(db.db_path()) + _size(f"{db.db_path()}-wal")

    def run():
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    await asyncio.to_thread(run)
    after = _size(db.db_path()) + _size(f"{db.db_path()}-wal")
    return f"Compacted the database: {before / 1e6:.1f} MB to {after / 1e6:.1f} MB."


async def _clear_cache(request, body) -> str:
    name = body.get("store")
    if name not in CACHES:
        raise BadRequest("Unknown cache.")
    store = storage.REGISTRY.get(name)
    if store is None:
        raise BadRequest("That cache isn't in use right now.")
    count = len(store.items())
    await asyncio.to_thread(store.clear)
    return f"Emptied {CACHES[name][0]} ({count} entries). They're looked up again when needed."


async def _sync_commands(request, body) -> str:
    bot = request.app[BOT_KEY]
    guild = auth.guild_for(bot)
    if guild is None:
        raise BadRequest("The bot isn't connected to the server.")
    bot.tree.copy_global_to(guild=guild)
    try:
        synced = await bot.tree.sync(guild=guild)
    except discord.HTTPException as exc:
        raise BadRequest(f"Discord refused: {exc.text or exc.status}") from None
    return f"Re-registered {len(synced)} slash commands in {guild.name}."


async def _legacy_buttons(request, body) -> str:
    updated, unreachable = await announcements.remove_legacy_buttons(request.app[BOT_KEY])
    text = f"Re-rendered {updated} published announcements without buttons."
    if unreachable:
        text += f" {len(unreachable)} couldn't be reached (deleted, or the bot lost access)."
    return text


async def _logout_others(request, body) -> str:
    removed = auth.delete_other_sessions(request.cookies.get(auth.SESSION_COOKIE))
    return f"Logged out {removed} other dashboard session{'s' if removed != 1 else ''}."


ACTIONS = {
    "apply-retention": (_apply_retention, "Applied log retention"),
    "compact": (_compact, "Compacted the database"),
    "clear-cache": (_clear_cache, "Cleared an API cache"),
    "sync-commands": (_sync_commands, "Re-synced the slash commands"),
    "legacy-buttons": (_legacy_buttons, "Removed old Edit buttons from announcements"),
    "logout-others": (_logout_others, "Logged out all other dashboard sessions"),
}


async def run_action(request):
    denied = forbidden_unless(request, RUN_LEVEL, "maintenance tools (admins only)")
    if denied:
        return denied
    name = request.match_info["action"]
    if name not in ACTIONS:
        return error("Unknown tool", 404)
    handler, summary = ACTIONS[name]
    try:
        body = await read_json(request)
        result = await handler(request, body)
    except BadRequest as exc:
        return error(str(exc), 400)
    target = body.get("store") if name == "clear-cache" else None
    audit(request, "maintenance", f"maintenance.{name}", summary + (f" ({CACHES[target][0]})" if target else ""),
          target=target, details={"result": result})
    return json_response({"result": result})


def register(app):
    app.router.add_get("/api/maintenance", overview)
    app.router.add_get("/api/maintenance/backup", backup)
    app.router.add_post("/api/maintenance/{action}", run_action)
