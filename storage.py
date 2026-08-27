"""
Tiny JSON-file store mapping review-message-id -> application info.
Good enough for a single-guild bot running on one machine. If you ever move
this to a real server and want something more robust, swap this out for
sqlite - the interface below is small on purpose so that's an easy change.
"""

import json
import os
from threading import Lock

_LOCK = Lock()


class ApplicationStore:
    def __init__(self, path: str = "applications.json"):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def add(self, message_id: int, applicant_id: int, character_name: str, **extra):
        with _LOCK:
            self._data[str(message_id)] = {
                "applicant_id": applicant_id,
                "character_name": character_name,
                "status": "pending",
                **extra,
            }
            self._save()

    def set(self, key: int, **fields):
        """
        Schema-free record set - unlike add(), doesn't assume any
        particular shape (no applicant_id/character_name/status). For
        records that don't fit the application shape but still want to
        share this same small JSON-store mechanism, e.g. announcements.
        """
        with _LOCK:
            self._data[str(key)] = fields
            self._save()

    def get(self, message_id: int):
        return self._data.get(str(message_id))

    def set_status(self, message_id: int, status: str):
        with _LOCK:
            if str(message_id) in self._data:
                self._data[str(message_id)]["status"] = status
                self._save()

    def update(self, message_id: int, **fields):
        with _LOCK:
            if str(message_id) in self._data:
                self._data[str(message_id)].update(fields)
                self._save()

    def delete(self, message_id: int):
        with _LOCK:
            self._data.pop(str(message_id), None)
            self._save()

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