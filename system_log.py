"""
The system log: what happened to the bot itself, as opposed to audit.py's
record of what people did with it.

Two kinds of entries, both in the shared database (db.py, table
`system_log`):

  - lifecycle: each step of a stop and a start, so a restart or update
    reads like a story: "New build detected", "Shutting down", "Dashboard
    web server stopped", "Bot stopped", then "Bot updated", "Bot
    starting", "Dashboard web server is up", "Back up and running". Also
    unexpected stops (detected on the next start) and Discord outages.
  - console: warnings and errors from any logger (this bot's own, discord.py,
    aiohttp, asyncio), with their tracebacks, captured by ConsoleCapture -
    the same lines that appear in the console, without needing SSH.

Safety:
  - Known secrets (bot token, OAuth client secret, API keys) and anything
    that looks like a token are replaced with "[redacted]" before storing,
    since entries are shown in the browser.
  - The same message repeating within REPEAT_WINDOW is folded into one row
    with a count, and at most MAX_NEW_PER_MINUTE new rows are written per
    minute, so an error loop can't flood the database.
  - Writing an entry never raises, and logging done while writing an entry
    is ignored (no feedback loops).

Settings: SYSTEM_LOG_LEVEL (what console output to keep) and
SYSTEM_LOG_RETENTION_DAYS (how long to keep it).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

import app_version
import db

log = logging.getLogger("wow-apply-bot.system_log")

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
CATEGORIES = ("lifecycle", "console")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

REPEAT_WINDOW_SECONDS = 600        # same message again within this = count + 1
MAX_NEW_PER_MINUTE = 30            # flood guard for new rows
MAX_MESSAGE_CHARS = 2000
MAX_TRACEBACK_CHARS = 8000
MAX_PAGE = 200
HEARTBEAT_SECONDS = 60
OUTAGE_THRESHOLD_SECONDS = 60      # Discord disconnects shorter than this are normal
PURGE_INTERVAL_SECONDS = 3600

# Loggers never captured: this module's own (feedback loop) and per-request
# web access logs (noise).
IGNORED_LOGGERS = ("wow-apply-bot.system_log", "aiohttp.access")

_listeners: List[Callable[[dict], None]] = []
# Repeats are only folded within one run of the bot, so each start's
# errors stay next to that start in the log.
_RUN_TOKEN = f"{os.getpid()}-{time.time():.0f}"
_state = threading.local()
_last_purge = 0.0


def now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def _setting(name: str, default):
    import config  # late import: config reads the database at import time

    return getattr(config, name, default)


# --- redaction -------------------------------------------------------------------

_SECRET_CACHE = {"values": [], "loaded": 0.0}
_SECRET_REFRESH_SECONDS = 300

_TOKEN_PATTERNS = [
    # Discord bot tokens.
    (re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"), "[redacted]"),
    # Authorization headers.
    (re.compile(r"\b(Bearer|Bot|Basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 [redacted]"),
    # key=value / "key": "value" for obviously secret keys.
    (re.compile(r"((?:client_secret|access_token|refresh_token|api_key|password|token|secret)[\"']?\s*[=:]\s*[\"']?)"
                r"[^\s\"'&,;}]{6,}", re.IGNORECASE), r"\1[redacted]"),
]


def _secret_values() -> List[str]:
    """Every configured secret (env and dashboard-stored), refreshed every few
    minutes so keys changed in the dashboard are covered too."""
    if time.monotonic() - _SECRET_CACHE["loaded"] < _SECRET_REFRESH_SECONDS and _SECRET_CACHE["loaded"]:
        return _SECRET_CACHE["values"]
    values = {os.environ.get(name, "") for name in ("DISCORD_TOKEN", "DISCORD_CLIENT_SECRET")}
    try:
        import secret_store

        values.update(secret_store.get(key) or "" for key in secret_store.SECRETS)
    except Exception:
        pass
    # Longest first, so a secret containing another is replaced whole.
    _SECRET_CACHE["values"] = sorted((v for v in values if len(v) >= 8), key=len, reverse=True)
    _SECRET_CACHE["loaded"] = time.monotonic()
    return _SECRET_CACHE["values"]


def redact(text: str) -> str:
    if not text:
        return text
    for value in _secret_values():
        if value in text:
            text = text.replace(value, "[redacted]")
    for pattern, replacement in _TOKEN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_details(details: dict) -> dict:
    return json.loads(redact(json.dumps(details, default=str)))


# --- writing -----------------------------------------------------------------------

def record(level: str, category: str, event: str, message: str, details: Optional[dict] = None,
           fingerprint: Optional[str] = None) -> Optional[dict]:
    """Stores one entry (or bumps the count of an identical recent one) and
    tells live listeners. Returns the entry, or None if it couldn't be
    stored. Never raises."""
    if getattr(_state, "busy", False):
        return None  # logging triggered by writing an entry: ignore
    _state.busy = True
    try:
        entry = _store(level, category, event, message, details, fingerprint)
    except Exception:
        entry = None
        log.exception("Failed to store a system log entry")  # ignored by ConsoleCapture
    finally:
        _state.busy = False
    if entry is not None:
        for listener in list(_listeners):
            try:
                listener(entry)
            except Exception:
                pass
        _maybe_purge()
    return entry


_flood = {"minute": 0, "written": 0, "dropped": 0}


def _store(level, category, event, message, details, fingerprint) -> Optional[dict]:
    level = level if level in LEVELS else "INFO"
    message = redact(message)[:MAX_MESSAGE_CHARS]
    details = _redact_details(details) if details else {}
    stamp = now()

    if fingerprint:
        window = (datetime.now(timezone.utc) - timedelta(seconds=REPEAT_WINDOW_SECONDS)).strftime(TIME_FORMAT)
        rows = db.query(
            "SELECT id FROM system_log WHERE fingerprint = ? AND last_seen >= ? ORDER BY id DESC LIMIT 1",
            (fingerprint, window),
        )
        if rows:
            db.execute(
                "UPDATE system_log SET count = count + 1, last_seen = ?, details = ? WHERE id = ?",
                (stamp, json.dumps(details) if details else None, rows[0]["id"]),
            )
            return get(rows[0]["id"])

    # Flood guard: count new rows per minute, note how many were dropped.
    minute = int(time.time() // 60)
    if minute != _flood["minute"]:
        dropped = _flood["dropped"]
        _flood.update(minute=minute, written=0, dropped=0)
        if dropped:
            _insert(stamp, "WARNING", "console", "system_log.dropped",
                    f"{dropped} more log message(s) were not stored: too many in one minute.", None, None)
    if category == "console" and _flood["written"] >= MAX_NEW_PER_MINUTE:
        _flood["dropped"] += 1
        return None
    _flood["written"] += 1
    return get(_insert(stamp, level, category, event, message, details, fingerprint))


def _insert(stamp, level, category, event, message, details, fingerprint) -> int:
    cursor = db.execute(
        "INSERT INTO system_log (created_at, last_seen, level, category, event, message, details, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (stamp, stamp, level, category, event, message, json.dumps(details) if details else None, fingerprint),
    )
    return cursor.lastrowid


def subscribe(listener: Callable[[dict], None]) -> Callable[[], None]:
    """Calls `listener(entry)` for every new or repeated entry; returns an
    unsubscribe function. Must be quick and must not log."""
    _listeners.append(listener)

    def unsubscribe():
        if listener in _listeners:
            _listeners.remove(listener)

    return unsubscribe


def purge(retention_days: Optional[int] = None) -> int:
    days = int(_setting("SYSTEM_LOG_RETENTION_DAYS", 90)) if retention_days is None else retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TIME_FORMAT)
    return db.execute("DELETE FROM system_log WHERE last_seen < ?", (cutoff,)).rowcount


def _maybe_purge():
    global _last_purge
    if time.monotonic() - _last_purge < PURGE_INTERVAL_SECONDS:
        return
    _last_purge = time.monotonic()
    try:
        purge()
    except Exception:
        pass


# --- console capture -------------------------------------------------------------------

class ConsoleCapture(logging.Handler):
    """Logging handler (on the root logger) that stores records at or above
    its level in the system log."""

    def emit(self, rec: logging.LogRecord):
        if rec.name.startswith(IGNORED_LOGGERS) or getattr(_state, "busy", False):
            return
        try:
            message = rec.getMessage()
            details = {"logger": rec.name, "where": f"{rec.module}:{rec.lineno}"}
            if rec.exc_info:
                details["traceback"] = logging.Formatter().formatException(rec.exc_info)[-MAX_TRACEBACK_CHARS:]
            elif rec.stack_info:
                details["traceback"] = rec.stack_info[-MAX_TRACEBACK_CHARS:]
            exc_type = rec.exc_info[0].__name__ if rec.exc_info and rec.exc_info[0] else ""
            # Identical messages (same run, logger, level and exception) are
            # folded into one entry with a count.
            fingerprint = hashlib.sha1(
                f"{_RUN_TOKEN}|{rec.name}|{rec.levelname}|{exc_type}|{message[:300]}".encode("utf-8", "replace")
            ).hexdigest()
            level = rec.levelname if rec.levelname in LEVELS else "WARNING"
            record(level, "console", rec.name, message, details, fingerprint)
        except Exception:
            self.handleError(rec)


_handler: Optional[ConsoleCapture] = None


def install(level: Optional[str] = None) -> Optional[ConsoleCapture]:
    """Starts capturing console output at `level` (default: the
    SYSTEM_LOG_LEVEL setting; "OFF" disables capture). Safe to call twice."""
    global _handler
    level = (level or _setting("SYSTEM_LOG_LEVEL", "WARNING") or "WARNING").upper()
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler = None
    if level == "OFF":
        return None
    _handler = ConsoleCapture(level=level if level in LEVELS else "WARNING")
    root.addHandler(_handler)
    return _handler


# --- lifecycle -------------------------------------------------------------------------

_run = {"shutdown_reason": None, "heartbeat": None, "disconnected_at": None, "ready_once": False,
        "started_monotonic": None, "stopping_monotonic": None, "down_seconds": None, "update_announced": False}

# Written by deploy/test/update.sh (next to the database) when it has pulled
# a new build and is about to restart the bot with it: the new version,
# optionally followed by a tab and its commit title. Docker's stop signal
# can't say why, so this is how the bot knows it's stopping for an update.
UPDATE_MARKER = "update-pending"


def _marker_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db.db_path())), UPDATE_MARKER)


def pending_update() -> Optional[dict]:
    """{"version", "title"} of a build waiting to be started, else None."""
    try:
        with open(_marker_path(), encoding="utf-8") as f:
            version, _, title = f.read().strip().partition("\t")
    except OSError:
        return None
    return {"version": version.strip()[:12] or "unknown", "title": title.strip() or None}


def _clear_update_marker():
    try:
        os.remove(_marker_path())
    except OSError:
        pass


def _when(stamp: Optional[str]) -> str:
    return stamp.replace("T", " ").replace("Z", " UTC") if stamp else ""


def _version_details() -> dict:
    info = app_version.INFO
    return {
        "version": info["version"],
        "title": info["title"],
        "built_at": info["built_at"],
        "commit_url": app_version.commit_url(info["version"]),
    }


def startup():
    """Call once at startup, before connecting to Discord. Records an
    unexpected stop of the previous run, a version change, and this start."""
    version = app_version.VERSION
    previous_state = db.get_meta("system_run_state")
    previous_version = db.get_meta("system_version")
    last_heartbeat = db.get_meta("system_heartbeat")
    stopped_at = db.get_meta("system_stopped_at")

    if previous_state == "running":
        when = f" Last sign of life: {_when(last_heartbeat)}." if last_heartbeat else ""
        record("ERROR", "lifecycle", "bot.crashed",
               "The bot stopped unexpectedly last time (crash, out of memory, or the process was killed)." + when,
               {"last_heartbeat": last_heartbeat, "version": previous_version})
        stopped_at = last_heartbeat
    _run["down_seconds"] = _seconds_since(stopped_at)

    if previous_version and previous_version != version:
        title = app_version.INFO["title"]
        record("INFO", "lifecycle", "bot.updated",
               f"Bot updated to the latest version: {version} (was {previous_version})" + (f": {title}" if title else ""),
               {**_version_details(), "previous_version": previous_version,
                "compare_url": app_version.compare_url(previous_version, version),
                "commits": app_version.commits_between(previous_version, version)})
    _clear_update_marker()  # this start is the update it announced (or it's stale)

    record("INFO", "lifecycle", "bot.started", f"Bot starting (version {version})", _version_details())
    db.set_meta("system_run_state", "running")
    db.set_meta("system_version", version)
    db.set_meta("system_heartbeat", now())
    _run["started_monotonic"] = time.monotonic()


def _seconds_since(stamp: Optional[str]) -> Optional[float]:
    try:
        then = datetime.strptime(stamp, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())


def set_shutdown_reason(reason: str, *, overwrite: bool = True):
    """Say why the bot is about to stop (e.g. a dashboard restart), so the
    'stopped' entry explains it."""
    if overwrite or not _run["shutdown_reason"]:
        _run["shutdown_reason"] = reason


def stopping(reason: str):
    """Call when a stop begins: records why (and a pending update, if
    deploy/test/update.sh announced one) before anything shuts down."""
    if _run["stopping_monotonic"] is not None:
        return  # a second signal while already stopping
    set_shutdown_reason(reason)
    _run["stopping_monotonic"] = time.monotonic()
    update = pending_update()
    if update and update["version"] != app_version.VERSION and not _run["update_announced"]:
        _run["update_announced"] = True
        _run["shutdown_reason"] = f"updating to version {update['version']}"
        record("INFO", "lifecycle", "update.detected",
               f"New build detected: version {update['version']}" + (f" ({update['title']})" if update["title"] else "")
               + f". Restarting to update from {app_version.VERSION}.", {"version": update["version"],
                                                                         "previous_version": app_version.VERSION})
    record("INFO", "lifecycle", "bot.stopping", f"Shutting down: {_run['shutdown_reason']}.",
           {"reason": _run["shutdown_reason"]})


def web_started(address: str):
    record("INFO", "lifecycle", "web.started", f"Dashboard web server is up ({address})", {"address": address})


def web_stopped(closed_tabs: int = 0):
    tabs = f", closed {closed_tabs} open dashboard tab{'s' if closed_tabs != 1 else ''}" if closed_tabs else ""
    record("INFO", "lifecycle", "web.stopped", f"Dashboard web server stopped{tabs}.", {"closed_tabs": closed_tabs})


def stop_requested() -> bool:
    """True once something (a signal, a dashboard restart) asked the bot to stop."""
    return bool(_run["shutdown_reason"])


def shutdown(default_reason: str = "shut down normally"):
    """Call once when the bot stops cleanly."""
    if _run["heartbeat"] is not None:
        _run["heartbeat"].cancel()
    reason = _run["shutdown_reason"] or default_reason
    took = time.monotonic() - _run["stopping_monotonic"] if _run["stopping_monotonic"] is not None else None
    record("INFO", "lifecycle", "bot.stopped",
           f"Bot stopped: {reason}." + (f" Shutting down took {_short(took)}." if took is not None else ""),
           {"reason": reason, **({"seconds": round(took, 1)} if took is not None else {})})
    try:
        db.set_meta("system_stopped_at", now())
        db.set_meta("system_run_state", "stopped")
    except Exception:
        log.exception("Couldn't save the bot's stopped state")


async def _heartbeat():
    """Lets the next start tell roughly when an unexpected stop happened."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await asyncio.to_thread(db.set_meta, "system_heartbeat", now())
        except Exception:
            log.exception("System log heartbeat failed")


def attach(bot):
    """Starts the heartbeat and records Discord connection events. Call
    from inside the running event loop (e.g. in bot.setup_hook)."""
    _run["heartbeat"] = asyncio.get_running_loop().create_task(_heartbeat())

    async def on_ready():
        if not _run["ready_once"]:
            _run["ready_once"] = True
            took = time.monotonic() - (_run["started_monotonic"] or time.monotonic())
            down = _run["down_seconds"]
            record("INFO", "lifecycle", "discord.connected",
                   f"Bot is {'back ' if down is not None else ''}up and running: connected to Discord as {bot.user} "
                   f"({took:.0f}s after starting" + (f", offline for {_duration(down)})" if down is not None else ")"),
                   {"startup_seconds": round(took, 1),
                    **({"offline_seconds": round(down)} if down is not None else {})})
        _reconnected()

    async def on_disconnect():
        if _run["disconnected_at"] is None and not bot.is_closed():
            _run["disconnected_at"] = time.monotonic()

    async def on_resumed():
        _reconnected()

    def _reconnected():
        started = _run["disconnected_at"]
        _run["disconnected_at"] = None
        if started is not None:
            down = time.monotonic() - started
            if down >= OUTAGE_THRESHOLD_SECONDS:
                record("WARNING", "lifecycle", "discord.outage",
                       f"The Discord connection was down for {_duration(down)} and has recovered.",
                       {"seconds": round(down)})

    bot.add_listener(on_ready, "on_ready")
    bot.add_listener(on_disconnect, "on_disconnect")
    bot.add_listener(on_resumed, "on_resumed")


def _short(seconds: float) -> str:
    return "under 0.1s" if seconds < 0.1 else f"{seconds:.1f}s"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


# --- reading ----------------------------------------------------------------------------

def serialize(row) -> dict:
    entry = dict(row)
    entry.pop("fingerprint", None)
    entry["details"] = json.loads(entry["details"]) if entry.get("details") else {}
    return entry


def get(entry_id: int) -> Optional[dict]:
    rows = db.query("SELECT * FROM system_log WHERE id = ?", (entry_id,))
    return serialize(rows[0]) if rows else None


def _where_clause(filters: dict) -> Tuple[str, list]:
    clauses, params = [], []
    if filters.get("category"):
        clauses.append("category = ?")
        params.append(filters["category"])
    if filters.get("levels"):
        clauses.append("level IN (" + ",".join("?" * len(filters["levels"])) + ")")
        params.extend(filters["levels"])
    if filters.get("since"):
        clauses.append("last_seen >= ?")
        params.append(filters["since"])
    q = (filters.get("q") or "").strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(message LIKE ? ESCAPE '\\' OR event LIKE ? ESCAPE '\\' OR details LIKE ? ESCAPE '\\')")
        params.extend([like] * 3)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def search(filters: Optional[dict] = None, *, before_id: Optional[int] = None, limit: int = 50) -> Tuple[list, bool]:
    """Newest first. Returns (entries, has_more)."""
    limit = max(1, min(int(limit), MAX_PAGE))
    where, params = _where_clause(dict(filters or {}))
    if before_id:
        where += (" AND " if where else " WHERE ") + "id < ?"
        params.append(int(before_id))
    rows = db.query(f"SELECT * FROM system_log{where} ORDER BY id DESC LIMIT ?", (*params, limit + 1))
    return [serialize(r) for r in rows[:limit]], len(rows) > limit


def summary() -> dict:
    """Headline numbers for the dashboard (Overview and System log page)."""
    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(TIME_FORMAT)
    week = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(TIME_FORMAT)
    counts = db.query(
        "SELECT "
        "  SUM(CASE WHEN level IN ('ERROR', 'CRITICAL') AND last_seen >= ? THEN count ELSE 0 END) AS errors, "
        "  SUM(CASE WHEN level = 'WARNING' AND last_seen >= ? THEN count ELSE 0 END) AS warnings, "
        "  SUM(CASE WHEN event = 'bot.started' AND created_at >= ? THEN 1 ELSE 0 END) AS starts, "
        "  SUM(CASE WHEN event = 'bot.crashed' AND created_at >= ? THEN 1 ELSE 0 END) AS crashes "
        "FROM system_log",
        (day, day, week, week),
    )[0]
    last_update = db.query("SELECT * FROM system_log WHERE event = 'bot.updated' ORDER BY id DESC LIMIT 1")
    last_error = db.query(
        "SELECT * FROM system_log WHERE level IN ('ERROR', 'CRITICAL') ORDER BY last_seen DESC LIMIT 1"
    )
    return {
        "errors_24h": counts["errors"] or 0,
        "warnings_24h": counts["warnings"] or 0,
        "starts_7d": counts["starts"] or 0,
        "crashes_7d": counts["crashes"] or 0,
        "last_update": serialize(last_update[0]) if last_update else None,
        "last_error_at": last_error[0]["last_seen"] if last_error else None,
        "capture_level": str(_setting("SYSTEM_LOG_LEVEL", "WARNING")).upper(),
        "retention_days": int(_setting("SYSTEM_LOG_RETENTION_DAYS", 90)),
    }
