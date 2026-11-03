"""
Maintenance commands for the SQLite database (db.py). Run from the bot's
folder, with the bot STOPPED for `export`/`import`:

  python db_tools.py info              what's in the database
  python db_tools.py export [folder]   write every store back out as
                                       <store>.json (default: ./json_export)
                                       - the rollback path from
                                       STORAGE_BACKEND=sqlite to json
  python db_tools.py backup [file]     consistent copy of the database,
                                       safe while the bot is running
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402 (BOT_DB_PATH may come from .env)


def info():
    if not db.exists():
        print(f"No database at {db.db_path()}")
        return
    print(f"Database: {os.path.abspath(db.db_path())}")
    for row in db.query("SELECT store, COUNT(*) AS n FROM records GROUP BY store ORDER BY store"):
        print(f"  store {row['store']:<32} {row['n']:>6} record(s)")
    overrides = db.query("SELECT COUNT(*) AS n FROM settings")[0]["n"]
    sessions = db.query("SELECT COUNT(*) AS n FROM web_sessions WHERE expires_at > datetime('now')")[0]["n"]
    print(f"  dashboard setting overrides: {overrides}")
    print(f"  active web sessions:         {sessions}")


def export(folder="json_export"):
    os.makedirs(folder, exist_ok=True)
    stores = [row["store"] for row in db.query("SELECT DISTINCT store FROM records")]
    for store in stores:
        rows = db.query("SELECT key, data FROM records WHERE store = ?", (store,))
        path = os.path.join(folder, f"{store}.json")
        with open(path, "w") as f:
            json.dump({row["key"]: json.loads(row["data"]) for row in rows}, f, indent=2)
        print(f"  wrote {path} ({len(rows)} records)")
    print("Done. To roll back: stop the bot, copy these files over the old ones, "
          "remove STORAGE_BACKEND=sqlite from .env, start the bot.")


def backup(target=None):
    target = target or f"crusader-backup-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(target) as dest:
        db.connection().backup(dest)
    print(f"Backed up to {target}")


if __name__ == "__main__":
    command, *args = sys.argv[1:] or ["info"]
    {"info": info, "export": export, "backup": backup}.get(command, lambda *a: print(__doc__))(*args)
