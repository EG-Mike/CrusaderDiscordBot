"""Shared test setup: every test gets its own empty SQLite database."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402


class DatabaseTestCase(unittest.TestCase):
    """Points db.py at a fresh database file for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = os.environ.get("BOT_DB_PATH")
        os.environ["BOT_DB_PATH"] = os.path.join(self._tmp.name, "test.db")
        db._CONN = None

    def tearDown(self):
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        if self._old_path is None:
            os.environ.pop("BOT_DB_PATH", None)
        else:
            os.environ["BOT_DB_PATH"] = self._old_path
        self._tmp.cleanup()
