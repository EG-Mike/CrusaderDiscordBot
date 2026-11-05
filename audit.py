"""
The audit trail: who did what with the bot, where and when.

Every user action ends up here as one structured row in the shared SQLite
database (db.py, table `audit_events`):

  - Discord: slash commands, context menus, buttons, dropdowns and modal
    submits, captured by cogs/audit_log.py (FEATURE_AUDIT_LOG_ENABLED).
  - Dashboard: logins, setting changes, API key changes, setup, restarts,
    audit exports, recorded by webapi/server.py. Always on while the
    dashboard runs, since the dashboard itself is a privileged tool.

Privacy: rows record who, what, where and when, never private text. Modal
field contents, applicant notes, announcement bodies and API keys are never
passed in. Settings changes are the agreed exception: they keep the old and
new value (see the settings history in the dashboard).

Each event is also written, in the original one-line text format, to the
`wow-apply-bot.audit` logger. cogs/audit_log.py attaches the rotating
audit.log file to that logger, so the plain-text file keeps working for
anyone who reads it on the server.

Readers (the dashboard's Audit log page and its CSV export) use `search()`,
`facets()` and `stats()`. Live listeners (the dashboard's WebSocket hub)
register with `subscribe()`.

Old rows are deleted after AUDIT_LOG_RETENTION_DAYS (0 = keep forever).
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import db

log = logging.getLogger("wow-apply-bot.audit_store")
# The text-file mirror (cogs/audit_log.py attaches the file handler).
text_logger = logging.getLogger("wow-apply-bot.audit")
text_logger.propagate = False
text_logger.setLevel(logging.INFO)

SOURCES = ("discord", "dashboard")

# kind -> human label. The dashboard has its own copy of these labels for
# display; this one is used by the CSV export.
KINDS = {
    "command": "Command",
    "context_menu": "Context menu",
    "button": "Button",
    "select": "Dropdown",
    "modal": "Form",
    "login": "Login",
    "setting": "Setting",
    "integration": "API key",
    "setup": "Setup",
    "bot": "Bot",
    "announcement": "Announcement",
    "bot_message": "Bot message",
    "emoji": "Emoji",
    "maintenance": "Maintenance",
    "poll": "Poll",
    "roster": "Roster",
    "audit": "Audit log",
    "other": "Other",
}

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_DETAILS_CHARS = 4000   # bigger details (huge JSON settings) are dropped
MAX_PAGE = 200
PURGE_INTERVAL_SECONDS = 3600

_listeners: List[Callable[[dict], None]] = []
_last_purge = 0.0


def now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def _retention_days() -> int:
    import config  # late import: config imports db-backed overrides

    try:
        return max(0, int(getattr(config, "AUDIT_LOG_RETENTION_DAYS", 365)))
    except (TypeError, ValueError):
        return 365


def _str_or_none(value) -> Optional[str]:
    return None if value is None or value == "" else str(value)


# --- writing ----------------------------------------------------------------------

def record(
    *,
    source: str,
    kind: str,
    action: str,
    summary: str,
    actor_id=None,
    actor_name: Optional[str] = None,
    channel_id=None,
    channel_name: Optional[str] = None,
    target=None,
    details: Optional[dict] = None,
    text_line: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Optional[dict]:
    """Stores one event and tells live listeners about it. Returns the
    stored event (as `serialize()` shapes it), or None if storing failed:
    auditing must never break the action being audited, so errors are
    logged, not raised."""
    try:
        if source not in SOURCES:
            raise ValueError(f"unknown audit source {source!r}")
        if kind not in KINDS:
            kind = "other"
        details_json = None
        if details:
            details_json = json.dumps(details, default=str)
            if len(details_json) > MAX_DETAILS_CHARS:
                details_json = json.dumps({"truncated": True})
        created_at = created_at or now()
        cursor = db.execute(
            "INSERT INTO audit_events (created_at, source, kind, action, summary, actor_id, actor_name, "
            "channel_id, channel_name, target, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                created_at, source, kind, action[:200], summary[:500],
                _str_or_none(actor_id), actor_name, _str_or_none(channel_id), channel_name,
                _str_or_none(target), details_json,
            ),
        )
        event = {
            "id": cursor.lastrowid, "created_at": created_at, "source": source, "kind": kind,
            "action": action[:200], "summary": summary[:500], "actor_id": _str_or_none(actor_id),
            "actor_name": actor_name, "channel_id": _str_or_none(channel_id), "channel_name": channel_name,
            "target": _str_or_none(target), "details": json.loads(details_json) if details_json else {},
        }
    except Exception:
        log.exception("Failed to store audit event (%s %s)", source, action)
        return None

    try:
        text_logger.info(text_line or to_text_line(event))
    except Exception:
        log.exception("Failed to write audit log line")

    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:
            log.exception("Audit listener failed")

    _maybe_purge()
    return event


def subscribe(listener: Callable[[dict], None]) -> Callable[[], None]:
    """Calls `listener(event)` for every new event; returns an unsubscribe
    function. Listeners run synchronously inside record(), so they must be
    quick (the dashboard's just schedules a broadcast)."""
    _listeners.append(listener)

    def unsubscribe():
        if listener in _listeners:
            _listeners.remove(listener)

    return unsubscribe


def purge(retention_days: Optional[int] = None) -> int:
    """Deletes events older than the retention period. Returns how many."""
    days = _retention_days() if retention_days is None else retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TIME_FORMAT)
    return db.execute("DELETE FROM audit_events WHERE created_at < ?", (cutoff,)).rowcount


def _maybe_purge():
    global _last_purge
    if time.monotonic() - _last_purge < PURGE_INTERVAL_SECONDS:
        return
    _last_purge = time.monotonic()
    try:
        removed = purge()
        if removed:
            log.info("Audit log: removed %d event(s) older than %d days", removed, _retention_days())
    except Exception:
        log.exception("Audit log purge failed")


# --- text-file format ----------------------------------------------------------------

def _who(event: dict) -> str:
    name = event.get("actor_name") or "unknown"
    username = (event.get("details") or {}).get("username")
    if username and username != name:
        name = f"{name} [{username}]"
    return f"{name} ({event.get('actor_id')})" if event.get("actor_id") else name


def _where(event: dict) -> str:
    if event["source"] == "dashboard":
        return "web dashboard"
    if event.get("channel_id"):
        return f"#{event['channel_name']}({event['channel_id']})" if event.get("channel_name") \
            else f"channel({event['channel_id']})"
    return event.get("channel_name") or "DM"


def to_text_line(event: dict) -> str:
    """The original tab-separated audit.log line for an event."""
    timestamp = event["created_at"].replace("T", " ")
    if event["source"] == "dashboard":
        what = f"WEB {event['action']}" + (f" {event['target']}" if event.get("target") else "")
    else:
        what = event["summary"]
    return "\t".join(_clean(part) for part in (timestamp, _who(event), _where(event), what))


def _clean(text: str) -> str:
    return str(text).replace("\t", " ").replace("\r", " ").replace("\n", "⏎")


# --- importing the old text file ------------------------------------------------------

_WHO_RE = re.compile(r"^(?P<name>.*?)(?: \[(?P<username>[^\]]*)\])? \((?P<id>\d+)\)$")
_CHANNEL_RE = re.compile(r"^#(?P<name>.*)\((?P<id>\d+)\)$")
_BARE_CHANNEL_RE = re.compile(r"^channel\((?P<id>\d+)\)$")
_COMPONENT_RE = re.compile(
    r'^(?P<type>BUTTON|SELECT) (?P<custom_id>\S+)(?: "(?P<label>.*?)")?(?: values=(?P<values>.*?))?(?: msg=(?P<msg>\d+))?$'
)
_CONTEXT_RE = re.compile(r'^CONTEXT_MENU "(?P<name>.*)" on (?P<kind>user|message) (?P<target>\d+)$')
_OPTION_START_RE = re.compile(r" [\w-]+=")


def parse_text_line(line: str) -> Optional[dict]:
    """One audit.log line -> keyword arguments for record()/the importer,
    or None if it isn't an audit line. Lines the parser doesn't recognise
    are kept as kind "other" rather than lost."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 4:
        return None
    timestamp, who, where, what = parts
    try:
        created_at = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%SZ").strftime(TIME_FORMAT)
    except ValueError:
        return None

    event = {"created_at": created_at, "source": "discord", "details": {}}
    match = _WHO_RE.match(who)
    if match:
        event["actor_id"] = match["id"]
        event["actor_name"] = match["name"]
        if match["username"]:
            event["details"]["username"] = match["username"]
    else:
        event["actor_name"] = who

    if where == "web dashboard":
        event["source"] = "dashboard"
    elif where == "DM":
        event["channel_name"] = "DM"
    else:
        match = _CHANNEL_RE.match(where) or _BARE_CHANNEL_RE.match(where)
        if match:
            event["channel_id"] = match["id"]
            event["channel_name"] = match.groupdict().get("name")

    parsed = _parse_what(what, event["source"])
    event["details"].update(parsed.pop("details_extra", None) or {})
    event.update(parsed)
    return event


# Wording for dashboard lines written before the database existed.
_LEGACY_WEB_SUMMARIES = {
    "settings.update": "Changed setting {}",
    "settings.reset": "Reset setting {} to its default",
    "integrations.set": "Saved API key {}",
    "integrations.remove": "Removed API key {}",
    "setup.complete": "Finished the setup guide",
    "bot.restart": "Restarted the bot",
}


def _parse_what(what: str, source: str) -> dict:
    if source == "dashboard" and what.startswith("WEB "):
        action, _, target = what[4:].partition(" ")
        kind = {"settings": "setting", "integrations": "integration", "setup": "setup",
                "bot": "bot"}.get(action.split(".")[0], "other")
        summary = _LEGACY_WEB_SUMMARIES.get(action, action + " {}").format(target).strip()
        return {"kind": kind, "action": action, "target": target or None, "summary": summary}
    if what.startswith("COMMAND /"):
        rest = what[len("COMMAND "):]
        match = _OPTION_START_RE.search(rest)
        path = rest[:match.start()] if match else rest
        details = {"options_text": rest[match.start() + 1:]} if match else {}
        return {"kind": "command", "action": path, "summary": rest, "details_extra": details}
    match = _CONTEXT_RE.match(what)
    if match:
        return {"kind": "context_menu", "action": match["name"], "target": match["target"],
                "summary": f'Used "{match["name"]}" on a {match["kind"]}'}
    match = _COMPONENT_RE.match(what)
    if match:
        kind = "button" if match["type"] == "BUTTON" else "select"
        label = match["label"]
        details = {"custom_id": match["custom_id"]}
        if label:
            details["label"] = label
        if match["values"]:
            details["values"] = match["values"].split(",")
        if match["msg"]:
            details["message_id"] = match["msg"]
        return {"kind": kind, "action": label or match["custom_id"], "target": match["msg"],
                "summary": component_summary(kind, label or match["custom_id"], details.get("values")),
                "details_extra": details}
    if what.startswith("MODAL "):
        custom_id, _, fields = what[len("MODAL "):].partition(" fields=")
        details = {"custom_id": custom_id}
        if fields:
            details["fields"] = fields.split(",")
        return {"kind": "modal", "action": custom_id, "summary": f"Submitted form {custom_id}",
                "details_extra": details}
    return {"kind": "other", "action": what.split(" ", 1)[0][:60] or "?", "summary": what}


def component_summary(kind: str, name: str, values=None) -> str:
    if kind == "button":
        return f'Clicked "{name}"'
    if values:
        return f'Picked {", ".join(values)} in "{name}"'
    return f'Used dropdown "{name}"'


def import_text_files(path: str, backup_count: int) -> int:
    """One-off import of an existing audit.log (and its rotated backups)
    into the database, oldest first, so history from before the database
    existed shows up on the dashboard. Runs once per installation (tracked
    in the `meta` table); events older than the retention period are
    skipped. Returns how many events were imported."""
    if db.get_meta("audit_text_imported_at"):
        return 0
    files = [f"{path}.{i}" for i in range(backup_count, 0, -1)] + [path]
    days = _retention_days()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TIME_FORMAT) if days else ""
    rows = []
    for file_path in files:
        if not os.path.exists(file_path):
            continue
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                event = parse_text_line(line)
                if event is None or event["created_at"] < cutoff:
                    continue
                details = event.get("details")
                rows.append((
                    event["created_at"], event["source"], event["kind"], event["action"][:200],
                    event["summary"][:500], event.get("actor_id"), event.get("actor_name"),
                    event.get("channel_id"), event.get("channel_name"), event.get("target"),
                    json.dumps(details) if details else None,
                ))
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO audit_events (created_at, source, kind, action, summary, actor_id, actor_name, "
            "channel_id, channel_name, target, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('audit_text_imported_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now(),),
        )
    return len(rows)


# --- reading -----------------------------------------------------------------------

def serialize(row) -> dict:
    event = dict(row)
    event["details"] = json.loads(event["details"]) if event.get("details") else {}
    return event


def parse_time(value: Optional[str]) -> Optional[str]:
    """ISO 8601 from the browser (any offset, or a bare date) -> the stored
    UTC format. Raises ValueError for anything else."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime(TIME_FORMAT)


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where_clause(filters: Dict[str, Optional[str]]) -> Tuple[str, list]:
    clauses, params = [], []
    for column in ("source", "kind", "actor_id", "channel_id"):
        if filters.get(column):
            clauses.append(f"{column} = ?")
            params.append(str(filters[column]))
    if filters.get("since"):
        clauses.append("created_at >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        clauses.append("created_at < ?")
        params.append(filters["until"])
    q = (filters.get("q") or "").strip()
    if q:
        searchable = ("summary", "action", "actor_name", "channel_name", "target", "actor_id")
        clauses.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in searchable) + ")")
        params.extend([_like(q)] * len(searchable))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def search(filters: Optional[dict] = None, *, before_id: Optional[int] = None, limit: int = 50) -> Tuple[list, bool]:
    """Newest first. Returns (events, has_more). Page with before_id = the
    last event's id."""
    filters = dict(filters or {})
    limit = max(1, min(int(limit), MAX_PAGE))
    where, params = _where_clause(filters)
    if before_id:
        where += (" AND " if where else " WHERE ") + "id < ?"
        params.append(int(before_id))
    rows = db.query(f"SELECT * FROM audit_events{where} ORDER BY id DESC LIMIT ?", (*params, limit + 1))
    return [serialize(r) for r in rows[:limit]], len(rows) > limit


def iter_all(filters: Optional[dict] = None, cap: int = 10000) -> Iterable[dict]:
    """Every matching event, newest first, up to `cap` (for exports)."""
    where, params = _where_clause(dict(filters or {}))
    rows = db.query(f"SELECT * FROM audit_events{where} ORDER BY id DESC LIMIT ?", (*params, cap))
    return [serialize(r) for r in rows]


def facets(limit: int = 200) -> dict:
    """Who and where appears in the log, most active first: the options for
    the dashboard's member and channel pickers. Names are the most recent
    ones seen (SQLite returns the bare columns of the MAX(id) row)."""
    actors = db.query(
        "SELECT actor_id, actor_name, MAX(id) AS last_id, COUNT(*) AS n FROM audit_events "
        "WHERE actor_id IS NOT NULL GROUP BY actor_id ORDER BY n DESC LIMIT ?", (limit,),
    )
    channels = db.query(
        "SELECT channel_id, channel_name, MAX(id) AS last_id, COUNT(*) AS n FROM audit_events "
        "WHERE channel_id IS NOT NULL GROUP BY channel_id ORDER BY n DESC LIMIT ?", (limit,),
    )
    kinds = db.query("SELECT kind, COUNT(*) AS n FROM audit_events GROUP BY kind ORDER BY n DESC")
    return {
        "actors": [{"id": r["actor_id"], "name": r["actor_name"], "count": r["n"]} for r in actors],
        "channels": [{"id": r["channel_id"], "name": r["channel_name"], "count": r["n"]} for r in channels],
        "kinds": [{"kind": r["kind"], "count": r["n"]} for r in kinds],
    }


def stats() -> dict:
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).strftime(TIME_FORMAT)
    week = (now - timedelta(days=7)).strftime(TIME_FORMAT)
    row = db.query(
        "SELECT "
        "  SUM(created_at >= ?) AS day, "
        "  COUNT(*) AS week, "
        "  COUNT(DISTINCT actor_id) AS members, "
        "  SUM(source = 'dashboard') AS dashboard "
        "FROM audit_events WHERE created_at >= ?",
        (day, week),
    )[0]
    total = db.query("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM audit_events")[0]
    return {
        "events_24h": row["day"] or 0,
        "events_7d": row["week"] or 0,
        "members_7d": row["members"] or 0,
        "dashboard_7d": row["dashboard"] or 0,
        "total": total["n"],
        "oldest": total["oldest"],
        "retention_days": _retention_days(),
    }
