"""
Tiny key -> record store, shared by every feature (applications,
announcements, polls, raid summaries, API caches, ...).

Two interchangeable backends, picked by the STORAGE_BACKEND env var:

  - "json" (default): one JSON file per store, exactly as before.
  - "sqlite": one row per record in db.py's shared database. On first use
    each store imports its old JSON file once (the JSON file is left
    untouched as a backup); see db_tools.py to export back to JSON if you
    ever need to roll back.

Either way every record is also kept in memory, so reads cost nothing and
callers never notice which backend is active. Writes only touch the
changed record on SQLite instead of rewriting a whole file.
"""

import json
import logging
import os
from threading import Lock

log = logging.getLogger("wow-apply-bot.storage")

_LOCK = Lock()


def backend() -> str:
    value = os.environ.get("STORAGE_BACKEND", "json").strip().lower()
    return value if value in ("json", "sqlite") else "json"


def store_name_for_path(path: str) -> str:
    """'applications.json' -> 'applications' (the SQLite store name)."""
    base = os.path.basename(path)
    return base[:-5] if base.endswith(".json") else base


# Every store created in this process, by name - lets the web dashboard
# read the same live in-memory data the cogs use (one source of truth).
REGISTRY = {}


class ApplicationStore:
    def __init__(self, path: str = "applications.json"):
        self.path = path
        self.name = store_name_for_path(path)
        self.backend = backend()
        self._data = self._load()
        REGISTRY[self.name] = self

    # --- persistence ---------------------------------------------------------

    def _load(self) -> dict:
        if self.backend == "sqlite":
            return self._load_sqlite()
        return self._load_json()

    def _load_json(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {}

    def _load_sqlite(self) -> dict:
        import db

        already_imported = db.query("SELECT 1 FROM imported_stores WHERE store = ?", (self.name,))
        if not already_imported:
            data = self._load_json()
            with db.transaction() as conn:
                for key, record in data.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO records (store, key, data) VALUES (?, ?, ?)",
                        (self.name, str(key), json.dumps(record)),
                    )
                conn.execute(
                    "INSERT INTO imported_stores (store, source_path) VALUES (?, ?)",
                    (self.name, os.path.abspath(self.path) if os.path.exists(self.path) else None),
                )
            if data:
                log.info("Imported %d record(s) from %s into SQLite store %r", len(data), self.path, self.name)
            return data

        rows = db.query("SELECT key, data FROM records WHERE store = ?", (self.name,))
        return {row["key"]: json.loads(row["data"]) for row in rows}

    def _persist(self, key):
        """Save one record (or delete it, if it's no longer in memory)."""
        if self.backend == "json":
            self._save()
            return
        import db

        key = str(key)
        if key in self._data:
            db.execute(
                "INSERT OR REPLACE INTO records (store, key, data) VALUES (?, ?, ?)",
                (self.name, key, json.dumps(self._data[key])),
            )
        else:
            db.execute("DELETE FROM records WHERE store = ? AND key = ?", (self.name, key))

    def _persist_all(self):
        if self.backend == "json":
            self._save()
            return
        import db

        with db.transaction() as conn:
            conn.execute("DELETE FROM records WHERE store = ?", (self.name,))
            for key, record in self._data.items():
                conn.execute(
                    "INSERT INTO records (store, key, data) VALUES (?, ?, ?)",
                    (self.name, key, json.dumps(record)),
                )

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    # --- public interface (unchanged) ---------------------------------------

    def add(self, message_id: int, applicant_id: int, character_name: str, **extra):
        with _LOCK:
            self._data[str(message_id)] = {
                "applicant_id": applicant_id,
                "character_name": character_name,
                "status": "pending",
                **extra,
            }
            self._persist(message_id)

    def set(self, key: int, **fields):
        """
        Schema-free record set - unlike add(), doesn't assume any
        particular shape (no applicant_id/character_name/status). For
        records that don't fit the application shape but still want to
        share this same small store mechanism, e.g. announcements.
        """
        with _LOCK:
            self._data[str(key)] = fields
            self._persist(key)

    def get(self, message_id: int):
        return self._data.get(str(message_id))

    def set_status(self, message_id: int, status: str):
        with _LOCK:
            if str(message_id) in self._data:
                self._data[str(message_id)]["status"] = status
                self._persist(message_id)

    def update(self, message_id: int, **fields):
        with _LOCK:
            if str(message_id) in self._data:
                self._data[str(message_id)].update(fields)
                self._persist(message_id)

    def delete(self, message_id: int):
        with _LOCK:
            self._data.pop(str(message_id), None)
            self._persist(message_id)

    def clear(self):
        """Wipes every record in this store - for a whole-store cache reset
        (e.g. wowhead.py's item/spell cache after a data-source change makes
        every previously-cached entry suspect, not just the failed ones),
        not something a per-record feature should reach for."""
        with _LOCK:
            self._data = {}
            self._persist_all()

    def items(self):
        """
        Every (key, record) pair currently in the store, as a read-only
        snapshot - e.g. for a one-off migration/cleanup pass over every
        record (see cogs/announcements.py's announce-fix-legacy-buttons).
        Keys come back exactly as stored (string form) - callers that need
        the numeric Discord message ID should int() them.
        """
        with _LOCK:
            return list(self._data.items())

    def find_latest_by_applicant(self, applicant_id: int):
        """
        Returns (message_id, record) for the most recent application by this
        applicant, or None. Message IDs are Discord snowflakes, which sort
        chronologically, so max() gives the latest.
        """
        matches = self.find_all_by_applicant(applicant_id)
        if not matches:
            return None
        return matches[-1]

    def find_all_by_applicant(self, applicant_id: int):
        """Returns [(message_id, record), ...] for every application by this
        applicant, oldest first (message IDs are chronological snowflakes)."""
        matches = [
            (int(mid), rec)
            for mid, rec in self._data.items()
            if rec.get("applicant_id") == applicant_id
        ]
        matches.sort(key=lambda pair: pair[0])
        return matches
