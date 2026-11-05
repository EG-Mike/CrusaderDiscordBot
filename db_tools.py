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
  python db_tools.py import-old <folder>
                                       merge JSON files from an older install
                                       (applications.json, polls.json,
                                       announcements.json, ...) into the
                                       database; records the database already
                                       has are kept as they are. Stop the bot
                                       first: it keeps these in memory.
"""

import json
import os
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
    audit_count = db.query("SELECT COUNT(*) AS n FROM audit_events")[0]["n"]
    print(f"  audit log entries:           {audit_count}")
    system_count = db.query("SELECT COUNT(*) AS n FROM system_log")[0]["n"]
    print(f"  system log entries:          {system_count}")
    for label, table in (("announcements", "announcements"), ("announcement templates", "announcement_templates"),
                         ("edited bot messages", "bot_messages"), ("icon emoji", "emoji_library"),
                         ("items/spells with an emoji", "emoji_aliases")):
        count = db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        print(f"  {label + ':':<29}{count}")


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


def import_old(folder=None):
    """Merges every <store>.json in folder into the database. Records whose
    key the database already has are left alone (the newer bot's version
    wins); announcements are merged into their own table, skipping messages
    already known."""
    if not folder or not os.path.isdir(folder):
        print("Give the folder with the old JSON files, e.g.: python db_tools.py import-old /data/old")
        return
    import announcements
    from storage import store_name_for_path

    db.connection()  # creates the tables on a fresh database
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"{name}: skipped, couldn't read it ({exc})")
            continue
        if not isinstance(data, dict):
            print(f"{name}: skipped, not a store file")
            continue
        store = store_name_for_path(path)
        if store == "announcements":
            added = announcements.import_legacy(list(data.items()), merge=True)
            print(f"{name}: {added} announcement(s) added")
            continue
        added = 0
        with db.transaction() as conn:
            for key, record in data.items():
                cursor = conn.execute("INSERT OR IGNORE INTO records (store, key, data) VALUES (?, ?, ?)",
                                      (store, str(key), json.dumps(record)))
                added += cursor.rowcount
        print(f"{name}: {added} of {len(data)} record(s) added to {store!r}"
              + (f" ({len(data) - added} already there)" if added < len(data) else ""))


def backup(target=None):
    target = target or f"crusader-backup-{datetime.now():%Y%m%d-%H%M%S}.db"
    db.backup_to(target)
    print(f"Backed up to {target}")


if __name__ == "__main__":
    command, *args = sys.argv[1:] or ["info"]
    commands = {"info": info, "export": export, "backup": backup, "import-old": import_old}
    commands.get(command, lambda *a: print(__doc__))(*args)
