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
  - emoji_library.py: the bot's WoW icon emoji and which items/spells use them.
  - roster.py: in-game characters, their levels and Discord links, and
    level-up events.

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

SCHEMA_VERSION = 10

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
    # The bot's WoW icon emoji (emoji_library.py): one row per icon image,
    # identified by Blizzard's icon file ID and/or the icon's name, so every
    # item/spell sharing an icon reuses one emoji.
    """
    CREATE TABLE IF NOT EXISTS emoji_library (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        emoji_id    TEXT NOT NULL UNIQUE,
        emoji_name  TEXT NOT NULL,
        animated    INTEGER NOT NULL DEFAULT 0,
        file_id     INTEGER,
        icon_name   TEXT,
        image_url   TEXT,
        source      TEXT,
        created_at  TEXT NOT NULL,
        created_by  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_emoji_library_file ON emoji_library(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_emoji_library_icon ON emoji_library(icon_name)",
    # Which items/spells use which library emoji (the reuse list).
    """
    CREATE TABLE IF NOT EXISTS emoji_aliases (
        kind       TEXT NOT NULL,
        game_id    INTEGER NOT NULL,
        name       TEXT NOT NULL,
        library_id INTEGER NOT NULL REFERENCES emoji_library(id) ON DELETE CASCADE,
        added_at   TEXT NOT NULL,
        added_by   TEXT,
        PRIMARY KEY (kind, game_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_emoji_aliases_library ON emoji_aliases(library_id)",
    # The guild roster (roster.py): in-game characters from Blizzard, per
    # game version and region, optionally linked to a Discord member.
    """
    CREATE TABLE IF NOT EXISTS roster_characters (
        game               TEXT NOT NULL,
        region             TEXT NOT NULL,
        character_id       INTEGER NOT NULL,
        slug               TEXT NOT NULL,
        name               TEXT NOT NULL,
        surname            TEXT,
        class_id           INTEGER,
        race_id            INTEGER,
        faction            TEXT,
        level              INTEGER,
        ruleset_id         INTEGER,
        guild_slug         TEXT,
        guild_rank         INTEGER,
        discord_id         INTEGER,
        is_main            INTEGER NOT NULL DEFAULT 0,
        linked_by          TEXT,
        linked_at          TEXT,
        main_spec_id       INTEGER,
        off_spec_id        INTEGER,
        last_login         INTEGER,
        profile_found      INTEGER,
        profile_checked_at TEXT,
        roster_seen_at     TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        PRIMARY KEY (game, region, character_id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_roster_slug ON roster_characters(game, region, slug)",
    "CREATE INDEX IF NOT EXISTS idx_roster_member ON roster_characters(discord_id)",
    # Changes worth telling people about (roster.py): level-ups now;
    # achievements can use the same table later (kind).
    """
    CREATE TABLE IF NOT EXISTS roster_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        game         TEXT NOT NULL,
        region       TEXT NOT NULL,
        character_id INTEGER NOT NULL,
        kind         TEXT NOT NULL,
        old_value    INTEGER,
        new_value    INTEGER,
        milestone    INTEGER,
        created_at   TEXT NOT NULL,
        announced_at TEXT,
        outcome      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_roster_events_pending ON roster_events(announced_at, milestone)",
    # Crafting (crafting.py): the recipes moderators feature in the crafting
    # channel, with what they make and the reagents, cached from Blizzard.
    # Recipes are game data, so they're kept per game version only.
    """
    CREATE TABLE IF NOT EXISTS crafting_featured (
        game            TEXT NOT NULL,
        recipe_id       INTEGER NOT NULL,
        name            TEXT NOT NULL,
        profession_id   INTEGER,
        profession_name TEXT,
        item_id         INTEGER,
        item_name       TEXT,
        quantity        REAL,
        reagents        TEXT,
        icon_url        TEXT,
        added_by        TEXT,
        added_at        TEXT NOT NULL,
        PRIMARY KEY (game, recipe_id)
    )
    """,
    # Statistics on the application card (card_stats.py): every statistic
    # the bot has seen on a character (Blizzard has no list of them), and
    # the ones moderators chose, in order. Per game version.
    """
    CREATE TABLE IF NOT EXISTS card_stats_seen (
        game     TEXT NOT NULL,
        stat_id  INTEGER NOT NULL,
        name     TEXT NOT NULL,
        category TEXT,
        PRIMARY KEY (game, stat_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS card_stats_chosen (
        game     TEXT NOT NULL,
        stat_id  INTEGER NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (game, stat_id)
    )
    """,
    # Links waiting for their character (roster.py): an approved applicant
    # whose character Blizzard didn't know yet (below level 10) is linked
    # as soon as the character shows up on the roster.
    """
    CREATE TABLE IF NOT EXISTS roster_pending_links (
        game         TEXT NOT NULL,
        region       TEXT NOT NULL,
        slug         TEXT NOT NULL,
        discord_id   INTEGER NOT NULL,
        main_spec_id INTEGER,
        off_spec_id  INTEGER,
        linked_by    TEXT,
        created_at   TEXT NOT NULL,
        PRIMARY KEY (game, region, slug)
    )
    """,
    # Rank sync (rank_sync.py): names for the guild's in-game ranks (Blizzard
    # only gives numbers, 0 = guild master) and which in-game rank members
    # with a Discord role should have. Per game, region and guild.
    """
    CREATE TABLE IF NOT EXISTS rank_sync_ranks (
        game       TEXT NOT NULL,
        region     TEXT NOT NULL,
        guild_slug TEXT NOT NULL,
        rank       INTEGER NOT NULL,
        name       TEXT NOT NULL,
        PRIMARY KEY (game, region, guild_slug, rank)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rank_sync_rules (
        game       TEXT NOT NULL,
        region     TEXT NOT NULL,
        guild_slug TEXT NOT NULL,
        role_id    INTEGER NOT NULL,
        rank       INTEGER NOT NULL,
        added_by   TEXT,
        PRIMARY KEY (game, region, guild_slug, role_id)
    )
    """,
    # Auction house prices (auction_prices.py): per auction house, each
    # item's cheapest and median price per unit (copper) and how many are
    # for sale, from Blizzard's latest snapshot; plus when that snapshot was
    # taken. Replaced whenever Blizzard has a newer snapshot.
    """
    CREATE TABLE IF NOT EXISTS auction_prices (
        house_key    TEXT NOT NULL,
        item_id      INTEGER NOT NULL,
        min_price    INTEGER NOT NULL,
        median_price INTEGER NOT NULL,
        quantity     INTEGER NOT NULL,
        listings     INTEGER NOT NULL,
        PRIMARY KEY (house_key, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auction_snapshots (
        house_key   TEXT PRIMARY KEY,
        snapshot_at TEXT,
        modified    TEXT,
        checked_at  TEXT NOT NULL,
        auctions    INTEGER NOT NULL DEFAULT 0,
        items       INTEGER NOT NULL DEFAULT 0
    )
    """,
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


# Columns added to existing tables after they were first created: (table,
# column, definition). Added on start when missing, so older databases
# catch up without a manual step.
_ADDED_COLUMNS = [
    ("roster_characters", "professions", "TEXT"),  # JSON: [{id, name, skill, max}], primary professions
    ("roster_characters", "known_recipes", "TEXT"),  # JSON: [recipe id, ...] of every profession (crafting.py)
    ("crafting_featured", "category", "TEXT"),       # the recipe's category in Blizzard's list
    ("crafting_featured", "difficulty", "REAL"),     # 0 (easiest) .. 1 (hardest) within its category
    ("crafting_featured", "position", "INTEGER"),    # a moderator's order within the profession; NULL = by difficulty
]


def _add_missing_columns(conn: sqlite3.Connection):
    for table, column, definition in _ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
            _add_missing_columns(conn)
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


def backup_to(target: str):
    """A consistent copy of the database at `target`, safe while the bot
    runs (SQLite's online backup)."""
    import sqlite3

    with _LOCK:
        dest = sqlite3.connect(target)
        try:
            connection().backup(dest)
        finally:
            dest.close()


def get_meta(key: str, default=None):
    rows = query("SELECT value FROM meta WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_meta(key: str, value: str):
    execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
