"""
Settings overrides, applied on top of deployment.py's defaults in this
order (later wins):

  1. config/deployment.py         - defaults, committed to git
  2. config/deployment_local.py   - optional, NOT committed (gitignored).
                                    Per-machine values, e.g. a test bot's
                                    test-server channel/role IDs.
  3. the `settings` table in the  - values changed in the web dashboard.
     SQLite database (db.py)        Only read if the database file already
                                    exists, so a deployment that never
                                    used the dashboard is unaffected.

Overrides are applied once at startup (config is imported before any cog
loads), so a dashboard change takes effect on the next restart - the
dashboard shows those as "pending restart".

Values are stored as JSON with a small tagging scheme so Python-only types
survive the round trip: sets, tuples, and dicts with non-string keys (e.g.
FRESH_EXEMPT_ROLE_IDS' int role-ID keys).
"""

import importlib
import json
import logging

log = logging.getLogger("wow-apply-bot.config")

# Keys that were overridden, and by what: {"KEY": "local" | "dashboard"}.
OVERRIDE_SOURCES = {}
# Each overridden key's deployment.py default, before any override.
DEFAULTS = {}


# --- typed JSON encoding ----------------------------------------------------

def encode(value):
    """Python value -> JSON-safe structure (tagged where JSON can't express it)."""
    if isinstance(value, (set, frozenset)):
        items = [encode(v) for v in value]
        try:
            items.sort(key=lambda v: (str(type(v)), v))
        except TypeError:
            pass
        return {"__set__": items}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(v) for v in value]}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value) and not any(k.startswith("__") for k in value):
            return {k: encode(v) for k, v in value.items()}
        return {"__dict__": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"__set__"}:
            return {decode(v) for v in value["__set__"]}
        if set(value) == {"__tuple__"}:
            return tuple(decode(v) for v in value["__tuple__"])
        if set(value) == {"__dict__"}:
            return {decode(k): decode(v) for k, v in value["__dict__"]}
        return {k: decode(v) for k, v in value.items()}
    return value


def dumps(value) -> str:
    return json.dumps(encode(value))


def loads(text: str):
    return decode(json.loads(text))


# --- plain-JSON editing (what the dashboard's editor shows/accepts) ----------

def to_plain(value):
    """Python value -> plain JSON a human can edit: sets/tuples become
    lists, non-string dict keys become strings."""
    if isinstance(value, (set, frozenset, tuple, list)):
        items = [to_plain(v) for v in value]
        if isinstance(value, (set, frozenset)):
            try:
                items.sort()
            except TypeError:
                pass
        return items
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    return value


class CoercionError(ValueError):
    pass


def _type_name(value) -> str:
    return {bool: "true/false", int: "a whole number", float: "a number", str: "text",
            list: "a list", set: "a list", tuple: "a list", dict: "an object"}.get(type(value), type(value).__name__)


def coerce_like(template, value, path="value"):
    """
    Converts plain JSON `value` back into the same shape as `template`
    (the setting's deployment.py default): lists back into sets/tuples,
    string dict keys back into ints, "12" into 12 where an int is expected.
    Raises CoercionError with a readable message on a mismatch.
    """
    if template is None:
        return value
    if isinstance(template, bool):
        if isinstance(value, bool):
            return value
        raise CoercionError(f"{path} must be true or false")
    if isinstance(template, int):
        if isinstance(value, bool):
            raise CoercionError(f"{path} must be {_type_name(template)}")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise CoercionError(f"{path} must be {_type_name(template)}")
    if isinstance(template, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise CoercionError(f"{path} must be a number")
    if isinstance(template, str):
        if isinstance(value, str):
            return value
        raise CoercionError(f"{path} must be text")
    if isinstance(template, (list, set, frozenset, tuple)):
        if not isinstance(value, list):
            raise CoercionError(f"{path} must be a list")
        item_template = next(iter(template), None)
        items = [coerce_like(item_template, v, f"{path}[{i}]") for i, v in enumerate(value)]
        if isinstance(template, (set, frozenset)):
            return set(items)
        if isinstance(template, tuple):
            return tuple(items)
        return items
    if isinstance(template, dict):
        if not isinstance(value, dict):
            raise CoercionError(f"{path} must be an object")
        key_template = next(iter(template), None)
        result = {}
        for k, v in value.items():
            key = coerce_like(key_template, k, f"{path} key {k!r}") if key_template is not None else k
            # Same key in the default -> use that entry as the template
            # (nested dicts like CURRENT_TIER mix value types per key);
            # otherwise any existing value's shape.
            if key in template:
                value_template = template[key]
            else:
                value_template = next(iter(template.values()), None)
            result[key] = coerce_like(value_template, v, f"{path}[{k!r}]")
        return result
    return value


# --- applying ----------------------------------------------------------------

def _set(package, key, value, source):
    if key not in DEFAULTS:
        DEFAULTS[key] = getattr(package, key, None)
    setattr(package, key, value)
    OVERRIDE_SOURCES[key] = source


def apply_all(package):
    """Called from config/__init__.py after deployment.py's defaults load."""
    try:
        local = importlib.import_module(".deployment_local", package.__name__)
    except ModuleNotFoundError as exc:
        if exc.name != f"{package.__name__}.deployment_local":
            raise
        local = None
    if local is not None:
        for key, value in vars(local).items():
            if key.isupper():
                _set(package, key, value, "local")

    try:
        import db
        if not db.exists():
            return
        rows = db.query("SELECT key, value FROM settings")
    except Exception:
        log.exception("Couldn't read dashboard settings overrides - using file defaults only")
        return
    for row in rows:
        try:
            _set(package, row["key"], loads(row["value"]), "dashboard")
        except Exception:
            log.exception("Ignoring unreadable dashboard override for %s", row["key"])
