"""db_tools.py import-old: merging an older install's JSON files."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from tests.helpers import DatabaseTestCase

import db
import db_tools


class ImportOldTests(DatabaseTestCase):
    def test_merge_keeps_newer_records(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "polls.json"), "w") as f:
                json.dump({"1": {"question": "Old"}, "2": {"question": "Only old"}}, f)
            with open(os.path.join(folder, "announcements.json"), "w") as f:
                json.dump({"10": {"title": "Hi", "body": "x", "channel_id": 5, "is_draft": False}}, f)
            db.connection()
            db.execute("INSERT INTO records (store, key, data) VALUES ('polls', '1', ?)",
                       (json.dumps({"question": "New"}),))
            out = io.StringIO()
            with redirect_stdout(out):
                db_tools.import_old(folder)
                db_tools.import_old(folder)  # a second run adds nothing
        rows = {r["key"]: json.loads(r["data"]) for r in db.query("SELECT key, data FROM records WHERE store='polls'")}
        self.assertEqual(rows, {"1": {"question": "New"}, "2": {"question": "Only old"}})
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM announcements")[0]["n"], 1)
        self.assertIn("polls.json: 1 of 2 record(s) added", out.getvalue())
        self.assertIn("announcements.json: 0 announcement(s) added", out.getvalue())


if __name__ == "__main__":
    unittest.main()
