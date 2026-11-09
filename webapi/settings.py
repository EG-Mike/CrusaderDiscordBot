"""
The dashboard's view of config/deployment.py: which settings exist, how to
group, label and edit them (webapi/settings_catalog.py), and saving and
resetting overrides in the database.

Saved values take effect on the next bot restart (see config/overrides.py),
so each setting reports both its *running* value and its *saved* value,
and the dashboard flags any difference as "pending restart".
"""

import os
import re

import config
import config.deployment as deployment
import db
from config import overrides
from webapi import settings_catalog as catalog

# Settings read at the moment they're used (not cached at startup), so a
# saved value is applied to the running bot straight away - no restart.
LIVE_PREFIXES = ("NOTIFY_", "NOTIFICATIONS_", "AUCTION_")
LIVE_KEYS = {"GUILD_DISPLAY_NAME"}


def is_live(key: str) -> bool:
    return key.startswith(LIVE_PREFIXES) or key in LIVE_KEYS


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

# Extra limits beyond "not negative": key -> (min, max).
RANGES = {
    "MAX_SCREENSHOTS": (1, 9),
    "PARSE_HIGHLIGHT_THRESHOLD": (1, 100),
    "POLL_DEFAULT_MAX_VOTES_PER_MEMBER": (1, 25),
    "POLL_DEFAULT_MAX_ADDS_PER_MEMBER": (1, 25),
    "LEVELING_REFRESH_MINUTES": (2, 1440),
    "GUILD_ROSTER_REFRESH_MINUTES": (15, 1440),
    "NOTIFY_ERRORS_EVERY_MINUTES": (1, 1440),
    "CRAFTING_CRAFTERS_PER_PROFESSION": (1, 50),
    "AUCTION_REFRESH_MINUTES": (5, 1440),
    "AUCTION_PRICE_COOLDOWN_SECONDS": (0, 3600),
}


def _entry(key: str) -> catalog.Entry:
    """The catalog entry; a setting missing from it (a new one) still shows,
    under Advanced, so nothing silently disappears."""
    return catalog.CATALOG.get(key) or catalog.Entry("other", key.replace("_", " ").capitalize(), advanced=True)


def feature_labels() -> dict:
    return {key: _entry(key).label for key in all_keys() if key.startswith("FEATURE_")}


def _kind_for(key: str, value) -> str:
    if _entry(key).choices and _entry(key).widget != "choice_list":
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


def _file_default(key: str):
    """Value before dashboard overrides: deployment_local.py's if it set
    one, else deployment.py's."""
    if overrides.OVERRIDE_SOURCES.get(key) == "dashboard":
        return overrides.DEFAULTS.get(key)
    if key in _STARTUP_VALUES:  # a live setting changed since startup: config holds the new value
        return _STARTUP_VALUES[key]
    return getattr(config, key)


# Values of live settings before this run changed them (save() applies
# them to config straight away, so config no longer holds the default).
_STARTUP_VALUES = {}


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
    """Every setting the dashboard shows (hidden ones excluded)."""
    return [k for k in vars(deployment) if k.isupper() and k not in catalog.HIDDEN]


def describe(key: str, saved=None) -> dict:
    running = getattr(config, key)
    file_default = _file_default(key)
    if saved is None:
        saved = _saved_overrides().get(key)
    saved_value = overrides.loads(saved["value"]) if saved else file_default
    entry = _entry(key)
    return {
        "key": key,
        "label": entry.label,
        "group": entry.group,
        "kind": _kind_for(key, file_default),
        "options": [{"value": v, "label": label} for v, label in entry.choices] if entry.choices else None,
        "help": entry.help,
        "advanced": entry.advanced,
        "widget": entry.widget,
        "unit": entry.unit,
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
        "live": is_live(key),
    }


def list_settings() -> dict:
    saved = _saved_overrides()
    order = {key: i for i, key in enumerate(catalog.CATALOG)}  # the catalog's order within each tab
    items = [describe(key, saved.get(key)) for key in sorted(all_keys(), key=lambda k: order.get(k, len(order)))]
    groups = [
        {"id": gid, "section": section, "label": label, "description": desc,
         "count": sum(1 for s in items if s["group"] == gid),
         "advanced_count": sum(1 for s in items if s["group"] == gid and s["advanced"])}
        for gid, section, label, desc in catalog.GROUPS
    ]
    other = [s for s in items if s["group"] == "other"]
    if other:
        groups.append({"id": "other", "section": "system", "label": "Other", "description": "New settings.",
                       "count": len(other), "advanced_count": len(other)})
    return {"sections": [{"id": sid, "label": label} for sid, label in catalog.SECTIONS],
            "tabs": _tabs(groups), "groups": groups, "settings": items}


def _tabs(groups: list) -> list:
    """The Settings page's menu: one tab per group, except COMBINED_TABS,
    which show several groups as subsections. In the order of their first
    group."""
    by_id = {g["id"]: g for g in groups}
    combined_of = {gid: tab for tab, (_, _, gids) in catalog.COMBINED_TABS.items() for gid in gids}
    tabs, seen = [], set()
    for group in groups:
        tab_id = combined_of.get(group["id"], group["id"])
        if tab_id in seen:
            continue
        seen.add(tab_id)
        if tab_id in catalog.COMBINED_TABS:
            label, description, gids = catalog.COMBINED_TABS[tab_id]
            members = [by_id[g] for g in gids if g in by_id]
        else:
            label, description, members = group["label"], group["description"], [group]
        tabs.append({"id": tab_id, "section": group["section"], "label": label, "description": description,
                     "groups": [g["id"] for g in members],
                     "count": sum(g["count"] for g in members),
                     "advanced_count": sum(g["advanced_count"] for g in members)})
    return tabs


class SettingError(ValueError):
    pass


def _role_ids(key: str, value) -> list:
    entry = _entry(key)
    if entry.widget == "role_map" and isinstance(value, dict):
        return [v for v in value.values() if v]
    if entry.widget == "role_set" and isinstance(value, dict):
        return list(value)
    return []


def validate(key: str, value, guild=None):
    """Plain JSON from the dashboard -> typed Python value, or SettingError."""
    if key not in all_keys():
        raise SettingError("Unknown setting")
    template = _file_default(key)
    label = _entry(key).label
    try:
        typed = overrides.coerce_like(template, value, path=label)
    except overrides.CoercionError as exc:
        raise SettingError(str(exc)) from None

    choices = _entry(key).choices
    allowed = [v for v, _ in choices or []]
    if _entry(key).widget == "choice_list":
        if not typed and template:  # a list whose default is empty may be empty too
            raise SettingError(f"{label}: pick at least one")
        if any(v not in allowed for v in typed) or len(set(typed)) != len(typed):
            raise SettingError(f"{label}: pick from the options, each once")
    elif choices and typed not in allowed:
        raise SettingError(f"{label}: pick one of the options")
    if key == "RAID_LOG_AUTO_SUMMARIZE_TIME" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", typed):
        raise SettingError("Use 24-hour HH:MM, e.g. 23:59")
    if isinstance(typed, int) and not isinstance(typed, bool):
        if typed < 0:
            raise SettingError(f"{label} can't be negative")
        low, high = RANGES.get(key, (None, None))
        if low is not None and not low <= typed <= high:
            raise SettingError(f"{label} must be between {low} and {high}")
    if guild is not None:
        kind = _kind_for(key, template)
        if isinstance(typed, int) and typed:
            if kind == "role" and guild.get_role(typed) is None:
                raise SettingError(f"No role with ID {typed} in {guild.name}")
            if kind == "channel" and guild.get_channel_or_thread(typed) is None:
                raise SettingError(f"No channel with ID {typed} in {guild.name}")
        for role_id in _role_ids(key, typed):
            if guild.get_role(int(role_id)) is None:
                raise SettingError(f"No role with ID {role_id} in {guild.name}")
    return typed


def save(key: str, typed_value, changed_by: str) -> dict:
    """Stores the override (or removes it, if it equals the file default)
    and records the change in settings_history."""
    saved = _saved_overrides().get(key)
    old = saved["value"] if saved else None
    is_default = _same(typed_value, _file_default(key))
    new = None if is_default else overrides.dumps(typed_value)
    if is_live(key):
        if key not in _STARTUP_VALUES and overrides.OVERRIDE_SOURCES.get(key) != "dashboard":
            _STARTUP_VALUES[key] = getattr(config, key)
        setattr(config, key, typed_value)  # applies to the running bot now
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
        missing.append({"key": "WCL_CLIENT_ID", "label": "WarcraftLogs API keys", "group": "api_keys"})
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
    if saved.get("FEATURE_LEVELING_ENABLED") and not saved.get("FEATURE_ROSTER_ENABLED"):
        warnings.append("Leveling needs the Guild roster feature enabled.")
    if saved.get("FEATURE_TIER_RETROSPECTIVE_ENABLED") and not saved.get("FEATURE_RAID_SUMMARY_ENABLED"):
        warnings.append("Tier recap reads raid summary data and needs the Raid summary feature enabled.")
    return warnings

