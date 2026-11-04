"""
API keys for external services, entered in the web dashboard instead of
.env.

Stored encrypted (Fernet: AES + HMAC) in the database's `secrets` table.
The encryption key lives in a separate file, secret.key, created
automatically next to the database with owner-only permissions. A copy
of the database alone (a backup, an export) therefore doesn't expose the
keys. Values are write-only from the dashboard's point of view: it can set,
replace, test or remove a key, but never reads one back.

Lookup order for each key: dashboard value, then an environment variable
with the same name (how keys were configured before the dashboard, which
keeps an existing .env working), then "" (not configured).
"""

import logging
import os

import db

log = logging.getLogger("wow-apply-bot.secrets")

# key -> (service, label, help)
SECRETS = {
    "WCL_CLIENT_ID": (
        "warcraftlogs", "WarcraftLogs client ID",
        "From https://www.warcraftlogs.com/api/clients/ (create a client; no redirect URL needed).",
    ),
    "WCL_CLIENT_SECRET": ("warcraftlogs", "WarcraftLogs client secret", "Shown once when you create the client."),
    "BLIZZARD_CLIENT_ID": (
        "blizzard", "Blizzard client ID",
        "Optional. From https://develop.battle.net/access/clients. Preferred over Wowhead for item and "
        "spell icons and names, and needed for the armory gear check.",
    ),
    "BLIZZARD_CLIENT_SECRET": ("blizzard", "Blizzard client secret", "Shown when you create the client."),
}

SERVICES = {
    "warcraftlogs": ("WarcraftLogs", True, ["WCL_CLIENT_ID", "WCL_CLIENT_SECRET"]),
    "blizzard": ("Blizzard API", False, ["BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET"]),
}


def _key_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db.db_path())), "secret.key")


_fernet = None


def _cipher():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet

        path = _key_path()
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
            log.info("Created encryption key for stored API keys at %s - back it up with the database", path)
        with open(path, "rb") as f:
            _fernet = Fernet(f.read().strip())
    return _fernet


def _stored(key: str):
    if not db.exists():
        return None
    rows = db.query("SELECT ciphertext, updated_at, updated_by FROM secrets WHERE key = ?", (key,))
    return rows[0] if rows else None


def get(key: str) -> str:
    row = _stored(key)
    if row is not None:
        try:
            return _cipher().decrypt(row["ciphertext"].encode()).decode()
        except Exception:
            log.error("Couldn't decrypt the stored %s - was secret.key replaced? Re-enter it in the dashboard.", key)
            return ""
    return os.environ.get(key, "").strip()


def set(key: str, value: str, changed_by: str):
    if key not in SECRETS:
        raise KeyError(key)
    token = _cipher().encrypt(value.strip().encode()).decode()
    db.execute(
        "INSERT INTO secrets (key, ciphertext, updated_at, updated_by) VALUES (?, ?, datetime('now'), ?) "
        "ON CONFLICT(key) DO UPDATE SET ciphertext = excluded.ciphertext, updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by",
        (key, token, changed_by),
    )


def delete(key: str):
    db.execute("DELETE FROM secrets WHERE key = ?", (key,))


def describe(key: str) -> dict:
    service, label, help_text = SECRETS[key]
    row = _stored(key)
    from_env = bool(os.environ.get(key, "").strip())
    return {
        "key": key,
        "service": service,
        "label": label,
        "help": help_text,
        "set": row is not None or from_env,
        "source": "dashboard" if row is not None else "env" if from_env else None,
        "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
    }


def configured(service: str) -> bool:
    return all(get(key) for key in SERVICES[service][2])
