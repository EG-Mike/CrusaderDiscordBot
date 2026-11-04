"""
The dashboard's view of config/deployment.py: which settings exist, how to
group and edit them, their help text (the comments above each constant in
deployment.py), and saving/resetting overrides in the database.

Saved values take effect on the next bot restart (see config/overrides.py),
so each setting reports both its *running* value and its *saved* value,
and the dashboard flags any difference as "pending restart".
"""

import ast
import io
import os
import re
import tokenize

import config
import config.deployment as deployment
import db
from config import overrides

# Settings shown but not editable from the dashboard.
READ_ONLY = {
    # Turning the dashboard off from the dashboard would lock everyone out.
    "FEATURE_WEB_DASHBOARD_ENABLED": "Set by WEB_DASHBOARD_ENABLED in .env (or config/deployment_local.py) - "
                                     "switching it off here would lock you out.",
    # Moving the audit log file is a server-side file operation.
    "AUDIT_LOG_PATH": "A file path on the server - change it in config/deployment_local.py.",
}

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

ENUMS = {
    "LOG_LEVEL": LOG_LEVELS,
    "LOG_DISCORD_LIBRARY_LEVEL": LOG_LEVELS,
    "SERVER_REGION": ["us", "eu", "kr", "tw", "cn"],
}

# Settings whose fallback comes from .env (see deployment.py's "Discord
# server" section).
SERVER_KEYS = [
    "MOD_ROLE_ID", "FRESH_ROLE_ID", "REVIEW_CHANNEL_ID", "GEAR_CHECK_CHANNEL_ID",
    "ANNOUNCEMENT_SANDBOX_CHANNEL_ID", "ATTENDANCE_CHANNEL_ID", "MODERATOR_CHANNEL_ID",
    "RAID_SUMMARY_FORUM_CHANNEL_ID", "RAID_LOGS_CHANNEL_ID", "RAID_LOGS_REPOST_CHANNEL_ID",
    "SERVER_SLUG", "SERVER_REGION", "GUILD_NAME",
]

# What each feature needs before it does anything: (setting, needed by
# feature flag or None = always). Shown on the Overview as setup to do.
REQUIRED = [
    ("MOD_ROLE_ID", None, "Moderator role (who can use mod commands and this dashboard)"),
    ("REVIEW_CHANNEL_ID", None, "Application review channel"),
    ("FRESH_ROLE_ID", None, "Role given to approved applicants"),
    ("SERVER_SLUG", None, "WarcraftLogs realm"),
    ("ANNOUNCEMENT_SANDBOX_CHANNEL_ID", "FEATURE_ANNOUNCEMENTS_ENABLED", "Announcement drafts channel"),
    ("ATTENDANCE_CHANNEL_ID", "FEATURE_ATTENDANCE_ENABLED", "Attendance channel"),
    ("RAID_SUMMARY_FORUM_CHANNEL_ID", "FEATURE_RAID_SUMMARY_ENABLED", "Raid summary forum"),
]

# (group id, label, description, predicate) - first match wins.
GROUPS = [
    ("server", "Discord server", "Channels, roles and realm the bot works with.",
     lambda k: k in SERVER_KEYS),
    ("general", "General", "Logging and diagnostics.",
     lambda k: k.startswith("LOG_") or k == "DEBUG_COMMANDS_ENABLED"),
    ("features", "Features", "Turn whole features on or off.",
     lambda k: k.startswith("FEATURE_")),
    ("applications", "Applications", "Gear-check applications, approval and archiving.",
     lambda k: k in {"MAX_SCREENSHOTS", "OXM_REGISTER_COMMAND_ID", "FRESH_EXEMPT_ROLE_IDS", "AUTO_ROLE_IDS",
                     "APPROVER_ROLE_ID", "ARMORY_BLOCK_ENABLED"} or k.startswith("ARCHIVE_")),
    ("attendance", "Attendance", "Attendance tracking and Regular-role eligibility.",
     lambda k: k.startswith(("ATTENDANCE_", "ROSTER_")) or k in {"REGULAR_ROLE_ID", "ORGANIZER_ROLE_ID"}),
    ("raid_logs", "Raid logs", "The #logs repost, tagging and auto-summarize automation.",
     lambda k: k.startswith("RAID_LOG_")),
    ("raid_tiers", "Raid tiers", "Current, previous and future tiers and their instances.",
     lambda k: k in {"CURRENT_TIER", "PREVIOUS_TIER", "FUTURE_TIERS", "TIER_SUB_INSTANCES", "NEW_TIER_LOG_THRESHOLD"}),
    ("raid_summary", "Raid summaries", "What raid summaries track, highlight, exclude and tag.",
     lambda k: k.startswith(("TRACKED_", "TOP_", "EXCLUDED_", "PARSE_")) or k in {
         "RAID_TIER_BANNERS", "CLEAR_STATUS_TAG_NAMES", "TIER_TAG_IDS", "RAID_TYPE_TAG_IDS"}),
    ("polls", "Polls", "Defaults for new polls.", lambda k: k.startswith("POLL_")),
    ("audit", "Audit log", "Who did what with the bot, and how long that history is kept.", lambda k: k.startswith("AUDIT_LOG_")),
    ("icons", "Icons & emoji", "Where class, role, spec and ability icons come from.",
     lambda k: "ICON" in k or "EMOJI" in k),
    ("other", "Other", "Everything else.", lambda k: True),
]


def _group_for(key: str) -> str:
    for group_id, _label, _desc, predicate in GROUPS:
        if predicate(key):
            return group_id
    return "other"


# Feature toggles: (label, what it does). Their deployment.py comment is one
# shared block, so each gets its own short explanation here instead.
FEATURES = {
    "FEATURE_ANNOUNCEMENTS_ENABLED": ("Announcements", "/announce and its draft, edit and publish flow."),
    "FEATURE_EMOJI_ADMIN_ENABLED": ("Emoji admin", "/add-emoji: add custom emoji from Wowhead links."),
    "FEATURE_ATTENDANCE_ENABLED": ("Attendance", "/checkattendance and all its subcommands."),
    "FEATURE_RAID_SUMMARY_ENABLED": ("Raid summaries", "/raidsummary and every raidsummary-* command."),
    "FEATURE_RAID_LOGS_ENABLED": ("Raid logs automation",
                                  "The #logs repost, tagging and auto-Summarize automation. "
                                  "Needs Raid summaries and Attendance."),
    "FEATURE_TIER_RETROSPECTIVE_ENABLED": ("Tier recap", "/tier-recap. Needs Raid summaries."),
    "FEATURE_POLLS_ENABLED": ("Polls", "/poll create and every poll's buttons."),
    "FEATURE_AUDIT_LOG_ENABLED": ("Audit log", "Records the commands, buttons and forms members use in Discord, "
                                                "for the Audit log page and /auditlog."),
    "FEATURE_WEB_DASHBOARD_ENABLED": ("Web dashboard", "This dashboard."),
}

LABELS = {
    "MOD_ROLE_ID": "Moderator role",
    "FRESH_ROLE_ID": "Fresh role (given on approval)",
    "GEAR_CHECK_CHANNEL_ID": "Gear-check (apply) channel",
    "ANNOUNCEMENT_SANDBOX_CHANNEL_ID": "Announcement drafts channel",
    "MODERATOR_CHANNEL_ID": "Moderator chat channel",
    "RAID_SUMMARY_FORUM_CHANNEL_ID": "Raid summary forum",
    "RAID_LOGS_CHANNEL_ID": "Raid logs channel (WCL webhook)",
    "RAID_LOGS_REPOST_CHANNEL_ID": "Raid logs repost channel",
    "SERVER_SLUG": "Realm (WarcraftLogs slug)",
    "SERVER_REGION": "Region",
    "GUILD_NAME": "Guild name on WarcraftLogs",
}

_WORD_CASE = {"id": "ID", "ids": "IDs", "oxm": "OXM", "url": "URL", "urls": "URLs", "wcl": "WCL"}


def _label_for(key: str) -> str:
    if key in FEATURES:
        return FEATURES[key][0]
    if key in LABELS:
        return LABELS[key]
    words = [_WORD_CASE.get(w, w) for w in key.lower().split("_")]
    # "Review channel ID" -> "Review channel": the dashboard shows a picker
    # with names, not the raw ID.
    if key.endswith(("_ROLE_ID", "_CHANNEL_ID")):
        words = words[:-1]
    words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words)


def _kind_for(key: str, value) -> str:
    if key in ENUMS:
        return "enum"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        if key.endswith("_ROLE_ID"):
            return "role"
        if key.endswith("_CHANNEL_ID"):
            return "channel"
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "text" if len(value) > 80 or "\n" in value else "string"
    return "json"


_HELP = None


def _reflow(lines: list) -> str:
    """Comment lines -> readable text: hard-wrapped lines are joined back
    into paragraphs; blank lines and list items ("- ...") keep their breaks."""
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append("\n\n")
        elif re.match(r"^([-*]|\d+[.)])\s", stripped) or not out or out[-1].endswith("\n"):
            out.append(("\n" if out and not out[-1].endswith("\n") else "") + stripped)
        else:
            out.append(" " + stripped)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _help_text() -> dict:
    """{KEY: comment block directly above its assignment in deployment.py}"""
    global _HELP
    if _HELP is not None:
        return _HELP
    with open(deployment.__file__, encoding="utf-8") as f:
        source = f.read()
    lines = source.splitlines()
    comments_by_line = {
        tok.start[0]: re.sub(r"^#\s?", "", tok.string).strip()
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT and lines[tok.start[0] - 1].lstrip()[:1] != "#"
    }
    result = {}
    previous = None
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id.isupper()):
                continue
            comment = []
            i = node.lineno - 2
            # Only column-0 comments: an indented one is the previous
            # constant's trailing-comment continuation, not this one's.
            while i >= 0 and lines[i].startswith("#"):
                text = lines[i].strip()
                if text.startswith("# ---"):
                    break
                comment.append(re.sub(r"^#\s?", "", text))
                i -= 1
            comment.reverse()
            # Trailing comment on the assignment's last line, plus indented
            # comment-only continuation lines below it:
            #   ATTENDANCE_WINDOW = 5   # how many of the most recent ...
            #                           # logs count toward eligibility
            trailing = comments_by_line.get(node.end_lineno)
            if trailing:
                comment.append(trailing)
                j = node.end_lineno  # 0-based index of the next line
                while j < len(lines) and re.match(r"^\s+#", lines[j]):
                    comment.append(re.sub(r"^\s*#\s?", "", lines[j]).strip())
                    j += 1
            text = _reflow(comment)
            # Constants sharing one comment block, e.g.
            #   # Audit log file and rotation
            #   AUDIT_LOG_PATH = ...
            #   AUDIT_LOG_MAX_BYTES = ...     <- inherits the block above
            line_above = lines[node.lineno - 2] if node.lineno >= 2 else ""
            if not text and previous and line_above.strip() and not line_above.lstrip().startswith("#"):
                text = result.get(previous, "")
            result[target.id] = text
            previous = target.id
    _HELP = result
    return result


def _file_default(key: str):
    """Value before dashboard overrides: deployment_local.py's if it set
    one, else deployment.py's."""
    if overrides.OVERRIDE_SOURCES.get(key) == "dashboard":
        return overrides.DEFAULTS.get(key)
    return getattr(config, key)


# Discord IDs (snowflakes) are bigger than JavaScript numbers can hold
# exactly (2^53), so a browser would silently round 1502459742461624360 to
# 1502459742461624300. Large ints go to the browser as strings; validate()
# turns them back into ints (coerce_like accepts numeric strings).
_JS_MAX_SAFE_INT = 2 ** 53 - 1


def js_safe(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > _JS_MAX_SAFE_INT:
        return str(value)
    if isinstance(value, list):
        return [js_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: js_safe(v) for k, v in value.items()}
    return value


def _plain(value):
    return js_safe(overrides.to_plain(value))


def _saved_overrides() -> dict:
    return {row["key"]: row for row in db.query("SELECT key, value, updated_at, updated_by FROM settings")}


def _same(a, b) -> bool:
    return overrides.dumps(a) == overrides.dumps(b)


def all_keys() -> list:
    return [k for k in vars(deployment) if k.isupper()]


def describe(key: str, saved=None) -> dict:
    running = getattr(config, key)
    file_default = _file_default(key)
    if saved is None:
        saved = _saved_overrides().get(key)
    saved_value = overrides.loads(saved["value"]) if saved else file_default
    return {
        "key": key,
        "label": _label_for(key),
        "group": _group_for(key),
        "kind": _kind_for(key, file_default),
        "options": ENUMS.get(key),
        "help": FEATURES[key][1] if key in FEATURES else _help_text().get(key, ""),
        "read_only": key in READ_ONLY,
        "read_only_reason": READ_ONLY.get(key),
        "value": _plain(saved_value),
        "running_value": _plain(running),
        "default": _plain(file_default),
        "default_source": (
            "local" if overrides.OVERRIDE_SOURCES.get(key) == "local"
            else "env" if key in SERVER_KEYS and os.environ.get(key)
            else "deployment"
        ),
        "overridden": saved is not None,
        "updated_at": saved["updated_at"] if saved else None,
        "updated_by": saved["updated_by"] if saved else None,
        "pending_restart": not _same(saved_value, running),
    }


def list_settings() -> dict:
    saved = _saved_overrides()
    items = [describe(key, saved.get(key)) for key in all_keys()]
    groups = [
        {"id": gid, "label": label, "description": desc, "count": sum(1 for s in items if s["group"] == gid)}
        for gid, label, desc, _ in GROUPS
    ]
    return {"groups": [g for g in groups if g["count"]], "settings": items}


class SettingError(ValueError):
    pass


def validate(key: str, value, guild=None):
    """Plain JSON from the dashboard -> typed Python value, or SettingError."""
    if key not in all_keys():
        raise SettingError(f"Unknown setting {key}")
    if key in READ_ONLY:
        raise SettingError(READ_ONLY[key])
    template = _file_default(key)
    try:
        typed = overrides.coerce_like(template, value, path=key)
    except overrides.CoercionError as exc:
        raise SettingError(str(exc)) from None

    if key in ENUMS and typed not in ENUMS[key]:
        raise SettingError(f"{key} must be one of {', '.join(ENUMS[key])}")
    if key == "RAID_LOG_AUTO_SUMMARIZE_TIME" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", typed):
        raise SettingError("Use 24-hour HH:MM, e.g. 23:59")
    if isinstance(typed, int) and not isinstance(typed, bool) and typed < 0:
        raise SettingError(f"{key} can't be negative")
    if guild is not None and isinstance(typed, int) and typed:
        kind = _kind_for(key, template)
        if kind == "role" and guild.get_role(typed) is None:
            raise SettingError(f"No role with ID {typed} in {guild.name}")
        if kind == "channel" and guild.get_channel_or_thread(typed) is None:
            raise SettingError(f"No channel with ID {typed} in {guild.name}")
    return typed


def save(key: str, typed_value, changed_by: str) -> dict:
    """Stores the override (or removes it, if it equals the file default)
    and records the change in settings_history."""
    saved = _saved_overrides().get(key)
    old = saved["value"] if saved else None
    is_default = _same(typed_value, _file_default(key))
    new = None if is_default else overrides.dumps(typed_value)
    if old == new:
        return describe(key)
    with db.transaction() as conn:
        if new is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, datetime('now'), ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (key, new, changed_by),
            )
        conn.execute(
            "INSERT INTO settings_history (key, old_value, new_value, changed_by) VALUES (?, ?, ?, ?)",
            (key, old, new, changed_by),
        )
    return describe(key)


def reset(key: str, changed_by: str) -> dict:
    if key in READ_ONLY:
        raise SettingError(READ_ONLY[key])
    return save(key, _file_default(key), changed_by)


def history(key: str, limit: int = 50) -> list:
    rows = db.query(
        "SELECT id, old_value, new_value, changed_at, changed_by FROM settings_history "
        "WHERE key = ? ORDER BY id DESC LIMIT ?",
        (key, limit),
    )

    def plain(text):
        return None if text is None else _plain(overrides.loads(text))

    return [
        {
            "id": row["id"],
            # None means "the file default" in both columns.
            "old_value": plain(row["old_value"]),
            "new_value": plain(row["new_value"]),
            "changed_at": row["changed_at"],
            "changed_by": row["changed_by"],
        }
        for row in rows
    ]


def pending_restart_keys() -> list:
    return [s["key"] for s in list_settings()["settings"] if s["pending_restart"]]


def missing_setup() -> list:
    """Required settings/API keys that are still unset (saved values)."""
    import secret_store

    saved = {s["key"]: s for s in list_settings()["settings"]}
    missing = [
        {"key": key, "label": label, "group": saved[key]["group"]}
        for key, feature, label in REQUIRED
        if (feature is None or saved[feature]["value"]) and saved[key]["value"] in (0, "0", "", None)
    ]
    if not secret_store.configured("warcraftlogs"):
        missing.append({"key": "WCL_CLIENT_ID", "label": "WarcraftLogs API keys", "group": "integrations"})
    return missing


def dependency_warnings() -> list:
    """Same feature-dependency checks bot.py logs at startup, against the
    SAVED values (what the next restart will use)."""
    saved = {s["key"]: s["value"] for s in list_settings()["settings"] if s["key"].startswith("FEATURE_")}
    warnings = []
    if saved.get("FEATURE_RAID_LOGS_ENABLED") and not saved.get("FEATURE_RAID_SUMMARY_ENABLED"):
        warnings.append("Raid logs automation needs the Raid summary feature enabled.")
    if saved.get("FEATURE_RAID_LOGS_ENABLED") and not saved.get("FEATURE_ATTENDANCE_ENABLED"):
        warnings.append("Raid logs automation needs the Attendance feature enabled.")
    if saved.get("FEATURE_TIER_RETROSPECTIVE_ENABLED") and not saved.get("FEATURE_RAID_SUMMARY_ENABLED"):
        warnings.append("Tier recap reads raid summary data and needs the Raid summary feature enabled.")
    return warnings

