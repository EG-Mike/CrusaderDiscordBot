"""
Shared SQLite database (crusader.db by default, override with BOT_DB_PATH).

Used by:
  - storage.py, when STORAGE_BACKEND=sqlite (opt-in - the default is still
    the original JSON files, so an existing deployment changes nothing
    until it explicitly switches).
  - the web dashboard (webapi/): login sessions, dashboard-edited settings
    and their change history, encrypted API keys (secret_store.py).
  - audit.py: the audit trail (who did what with the bot, and when).
  - system_log.py: bot lifecycle events and captured console errors.
  - announcements.py: announcements, their version history and templates.
  - bot_messages.py: edited texts of the bot's standard messages.

One process-wide connection guarded by a lock: the bot is a single asyncio
process and every query here is tiny, so a connection pool would be pure
overhead. WAL mode keeps a crash mid-write from corrupting the file and
lets `sqlite3 crusader.db` (or a backup) read while the bot is running.
"""

import os
import sqlite3
from threading import RLock

_LOCK = RLock()
_CONN = None

SCHEMA_VERSION = 4

_SCHEMA = [
    # Generic key -> JSON record store, one "store" per former JSON file
    # (applications, announcements, polls, ...). See storage.py.
    """
    CREATE TABLE IF NOT EXISTS records (
        store TEXT NOT NULL,
        key   TEXT NOT NULL,
        data  TEXT NOT NULL,
        PRIMARY KEY (store, key)
    )
    """,
    # Which stores have already been imported from their JSON file, so an
    # emptied store isn't re-imported from a stale JSON file on next start.
    """
    CREATE TABLE IF NOT EXISTS imported_stores (
        store       TEXT PRIMARY KEY,
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        source_path TEXT
    )
    """,
    # Dashboard-edited settings, overriding config/deployment.py's defaults.
    """
    CREATE TABLE IF NOT EXISTS settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        key        TEXT NOT NULL,
        old_value  TEXT,
        new_value  TEXT,
        changed_at TEXT NOT NULL DEFAULT (datetime('now')),
        changed_by TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_settings_history_key ON settings_history(key, id)",
    # API keys entered in the dashboard, encrypted (see secret_store.py).
    """
    CREATE TABLE IF NOT EXISTS secrets (
        key        TEXT PRIMARY KEY,
        ciphertext TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_by TEXT
    )
    """,
    # Small key/value facts about this installation (e.g. setup completed).
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # Audit trail (audit.py): one row per user action, from Discord or the
    # dashboard. Never holds private text (modal contents, notes, keys).
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at   TEXT NOT NULL,
        source       TEXT NOT NULL,
        kind         TEXT NOT NULL,
        action       TEXT NOT NULL,
        summary      TEXT NOT NULL,
        actor_id     TEXT,
        actor_name   TEXT,
        channel_id   TEXT,
        channel_name TEXT,
        target       TEXT,
        details      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_channel ON audit_events(channel_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_events(kind, id)",
    # System log (system_log.py): bot lifecycle events (started, stopped,
    # updated, crashed) and console warnings/errors. Repeats of the same
    # message are folded into one row (count, last_seen).
    """
    CREATE TABLE IF NOT EXISTS system_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        last_seen  TEXT NOT NULL,
        count      INTEGER NOT NULL DEFAULT 1,
        level      TEXT NOT NULL,
        category   TEXT NOT NULL,
        event      TEXT NOT NULL,
        message    TEXT NOT NULL,
        details    TEXT,
        fingerprint TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_system_log_created ON system_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_system_log_level ON system_log(level, id)",
    "CREATE INDEX IF NOT EXISTS idx_system_log_fingerprint ON system_log(fingerprint, id)",
    # Announcements (announcements.py). One row per announcement, draft or
    # published; message IDs link it to Discord (the /announce sandbox
    # draft and the published message).
    """
    CREATE TABLE IF NOT EXISTS announcements (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        status             TEXT NOT NULL DEFAULT 'draft',
        title              TEXT,
        body               TEXT NOT NULL DEFAULT '',
        color              TEXT,
        ping               TEXT,
        channel_id         TEXT,
        message_id         TEXT,
        sandbox_message_id TEXT,
        revision           INTEGER NOT NULL DEFAULT 1,
        created_at         TEXT NOT NULL,
        created_by         TEXT,
        updated_at         TEXT NOT NULL,
        updated_by         TEXT,
        published_at       TEXT,
        published_by       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_announcements_message ON announcements(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_announcements_sandbox ON announcements(sandbox_message_id)",
    # What each announcement looked like when it was published and after
    # every edit of the published message.
    """
    CREATE TABLE IF NOT EXISTS announcement_versions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
        event           TEXT NOT NULL,
        title           TEXT,
        body            TEXT NOT NULL,
        color           TEXT,
        saved_at        TEXT NOT NULL,
        saved_by        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_announcement_versions ON announcement_versions(announcement_id, id)",
    """
    CREATE TABLE IF NOT EXISTS announcement_templates (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        title      TEXT,
        body       TEXT NOT NULL DEFAULT '',
        color      TEXT,
        ping       TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT,
        updated_at TEXT NOT NULL,
        updated_by TEXT
    )
    """,
    # The bot's standard messages (bot_messages.py), as edited in the
    # dashboard. A missing row = the built-in default text.
    """
    CREATE TABLE IF NOT EXISTS bot_messages (
        key        TEXT PRIMARY KEY,
        title      TEXT,
        body       TEXT NOT NULL,
        color      TEXT,
        updated_at TEXT NOT NULL,
        updated_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_message_history (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        key      TEXT NOT NULL,
        title    TEXT,
        body     TEXT NOT NULL,
        color    TEXT,
        saved_at TEXT NOT NULL,
        saved_by TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bot_message_history ON bot_message_history(key, id)",
    # Web dashboard login sessions. Only a SHA-256 of the cookie token is
    # stored, so a leaked database file can't be replayed as a login.
    """
    CREATE TABLE IF NOT EXISTS web_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        username   TEXT NOT NULL,
        avatar     TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    )
    """,
]


def db_path() -> str:
    return os.environ.get("BOT_DB_PATH", "crusader.db")


def connection() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            directory = os.path.dirname(db_path())
            if directory:
                os.makedirs(directory, exist_ok=True)
            conn = sqlite3.connect(db_path(), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _CONN = conn
        return _CONN


def execute(sql: str, params=()):
    with _LOCK:
        return connection().execute(sql, params)


def query(sql: str, params=()) -> list:
    with _LOCK:
        return connection().execute(sql, params).fetchall()


def transaction():
    """`with db.transaction() as conn:` - BEGIN/COMMIT (ROLLBACK on error)
    under the shared lock."""
    return _Transaction()


class _Transaction:
    def __enter__(self):
        _LOCK.acquire()
        self.conn = connection()
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            _LOCK.release()
        return False


def exists() -> bool:
    """True if the database file is already there - used by config's
    override loader so a deployment that never enabled any of this doesn't
    get an empty crusader.db created just by importing config."""
    return os.path.exists(db_path())


def get_meta(key: str, default=None):
    rows = query("SELECT value FROM meta WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_meta(key: str, value: str):
    execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
