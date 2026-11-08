"""
The guild roster: in-game characters read from Blizzard (wow_api.py), their
levels, and which Discord member each belongs to. Shared by cogs/roster.py,
cogs/leveling.py and the dashboard (webapi/roster_api.py); it holds the
rules, they handle Discord and HTTP.

Where characters and levels come from:
  - the guild roster (sync_guild): every member of the configured guild at
    every level, a snapshot Blizzard refreshes every few hours;
  - character profiles (refresh_profiles): one character at a time, from
    level 10, updated by Blizzard when the player logs out. These are
    checked every few minutes, so level-ups show soon after logging out.
Characters are stored per game version and region (both settings), so a
switch to another game keeps the old ones apart.

Levels only ever go up here: a snapshot that's older than a profile can't
lower a level. Each increase is recorded in roster_events; one that passes
a milestone (LEVELING_MILESTONES) is waiting to be announced until
cogs/leveling.py posts it. The first time a character is seen, its level is
just recorded (no event), so turning the roster on doesn't announce the
whole guild at once.

Linking: a member links a character to themselves (if self-service is on),
a moderator links any character to anyone. A character linked to someone
can only be moved by a moderator. Each member has one main: the first
character they link, until they choose another. An approved application
whose character Blizzard doesn't know yet (below level 10) leaves a
pending link (remember_link), applied as soon as the character shows up
from the guild roster or its profile.

Professions (with skill and maximum; primary and secondary) and the recipes the
character knows are read from the character's professions document after a
profile check finds the player logged out since the last read; they're
shown on the posted roster and in the crafting channel (crafting.py).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

import config
import db
import gear
import wow_api

log = logging.getLogger("wow-apply-bot.roster")

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PROFILE_MIN_LEVEL = 10           # Blizzard has no public profile below this
ACTIVE_DAYS = 3                  # characters not logged in for longer ...
IDLE_CHECK_HOURS = 6             # ... are only checked this often
PROFILE_CONCURRENCY = 4          # requests to Blizzard at the same time
ANNOUNCE_WITHIN_HOURS = 24       # older level-ups (e.g. the bot was down) aren't announced late


class RosterError(Exception):
    """Something the person can fix; the message says what."""


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def _scope() -> Tuple[str, str]:
    return config.GAME_VERSION, wow_api.region()


def _milestone(old: Optional[int], new: Optional[int]) -> Optional[int]:
    """The highest milestone passed going from old to new level, if any."""
    if old is None or new is None or new <= old:
        return None
    passed = [m for m in (config.LEVELING_MILESTONES or []) if isinstance(m, int) and old < m <= new]
    return max(passed) if passed else None


def _row(row) -> dict:
    return dict(row) if row is not None else None


# --- reading -------------------------------------------------------------------------------------

def get(character_slug: str) -> Optional[dict]:
    game, region = _scope()
    rows = db.query("SELECT * FROM roster_characters WHERE game = ? AND region = ? AND slug = ?",
                    (game, region, character_slug))
    return _row(rows[0]) if rows else None


def characters(*, discord_id: Optional[int] = None) -> List[dict]:
    """Every tracked character (or one member's), highest level first."""
    game, region = _scope()
    sql = "SELECT * FROM roster_characters WHERE game = ? AND region = ?"
    params: list = [game, region]
    if discord_id is not None:
        sql += " AND discord_id = ?"
        params.append(int(discord_id))
    sql += " ORDER BY level IS NULL, level DESC, name"
    return [dict(r) for r in db.query(sql, params)]


def posted_characters() -> List[dict]:
    """What the roster posted in Discord lists: the guild's members (every
    linked character when no guild is set) from GUILD_ROSTER_POST_MIN_LEVEL,
    plus mains of any level, highest level first."""
    guild = wow_api.slug(config.GUILD_ROSTER_GUILD or "")
    min_level = int(config.GUILD_ROSTER_POST_MIN_LEVEL or 0)
    return [c for c in characters() if (c["guild_slug"] == guild if guild else c["discord_id"])
            and ((c["level"] or 0) >= min_level or c["is_main"])]


def leaderboard(limit: int = 10, *, mains_only: bool = False) -> List[dict]:
    """Highest levels first; with mains_only, alts (a member's non-main
    characters) are left out."""
    rows = [c for c in characters() if c["level"] is not None]
    if mains_only:
        rows = [c for c in rows if not c["discord_id"] or c["is_main"]]
    return rows[:limit]


def recent_events(limit: int = 30) -> List[dict]:
    game, region = _scope()
    rows = db.query(
        "SELECT e.*, c.name, c.surname, c.slug, c.class_id, c.discord_id, c.is_main FROM roster_events e "
        "LEFT JOIN roster_characters c ON c.game = e.game AND c.region = e.region AND c.character_id = e.character_id "
        "WHERE e.game = ? AND e.region = ? ORDER BY e.id DESC LIMIT ?", (game, region, limit))
    return [dict(r) for r in rows]


def display_name(c: dict) -> str:
    return f"{c['name']} {c['surname']}" if c.get("surname") else c["name"]


def professions(c: dict, *, primary_only: bool = False) -> List[dict]:
    """A stored character's professions: [{id, name, skill, max, primary}].
    Stored before secondary ones were kept, an entry without "primary" is a
    primary one."""
    try:
        found = json.loads(c.get("professions") or "[]")
    except ValueError:
        return []
    for p in found:
        p.setdefault("primary", True)
    return [p for p in found if p["primary"]] if primary_only else found


# --- storing what Blizzard says -------------------------------------------------------------------

def _store(found: dict, *, source: str) -> Optional[dict]:
    """Inserts or updates one character from Blizzard (wow_api.parse_character).
    Returns the level event it caused, if any."""
    if not found.get("character_id") or not found.get("name"):
        return None
    game, region = _scope()
    stamp = timestamp()
    seen_column = "roster_seen_at" if source == "roster" else "profile_checked_at"
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM roster_characters WHERE game = ? AND region = ? AND character_id = ?",
                           (game, region, found["character_id"])).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO roster_characters (game, region, character_id, slug, name, surname, class_id, race_id, "
                "faction, level, ruleset_id, guild_slug, guild_rank, last_login, profile_found, "
                f"{seen_column}, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game, region, found["character_id"], found["slug"], found["name"], found["surname"],
                 found["class_id"], found["race_id"], found["faction"], found["level"], found["ruleset_id"],
                 found["guild_slug"], found["guild_rank"], found["last_login"],
                 1 if source == "profile" else None, stamp, stamp, stamp))
            return None  # first sighting: just the baseline, nothing to announce
        old = dict(row)
        level = max(filter(None, [old["level"], found["level"]]), default=None)
        updates = {
            "slug": found["slug"], "name": found["name"], "surname": found["surname"] or old["surname"],
            "class_id": found["class_id"] or old["class_id"], "race_id": found["race_id"] or old["race_id"],
            "faction": found["faction"] or old["faction"], "level": level,
            "ruleset_id": found["ruleset_id"] or old["ruleset_id"],
            "last_login": found["last_login"] or old["last_login"], seen_column: stamp, "updated_at": stamp,
        }
        if source == "roster":
            updates.update(guild_slug=found["guild_slug"], guild_rank=found["guild_rank"])
        else:
            updates.update(profile_found=1, guild_slug=found["guild_slug"])
        conn.execute(f"UPDATE roster_characters SET {', '.join(f'{k} = ?' for k in updates)} "
                     "WHERE game = ? AND region = ? AND character_id = ?",
                     (*updates.values(), game, region, found["character_id"]))
        if old["level"] is None or level is None or level <= old["level"]:
            return None
        event = {"game": game, "region": region, "character_id": found["character_id"], "kind": "level",
                 "old_value": old["level"], "new_value": level, "milestone": _milestone(old["level"], level),
                 "created_at": stamp}
        cursor = conn.execute(
            "INSERT INTO roster_events (game, region, character_id, kind, old_value, new_value, milestone, created_at,"
            " announced_at, outcome) VALUES (:game, :region, :character_id, :kind, :old_value, :new_value, :milestone,"
            " :created_at, :announced_at, :outcome)",
            {**event, "announced_at": None if event["milestone"] else stamp,
             "outcome": None if event["milestone"] else "no milestone"})
        return {**event, "id": cursor.lastrowid}


def store_roster(members: List[dict], guild_slug: str) -> List[dict]:
    """Stores a guild roster snapshot; members that left lose their guild
    (but stay tracked if someone linked them). Returns the level events."""
    events = [e for e in (_store(m, source="roster") for m in members) if e]
    game, region = _scope()
    present = [m["character_id"] for m in members if m.get("character_id")]
    marks = ",".join("?" * len(present)) or "NULL"
    db.execute(f"UPDATE roster_characters SET guild_slug = NULL, guild_rank = NULL WHERE game = ? AND region = ? "
               f"AND guild_slug = ? AND character_id NOT IN ({marks})", (game, region, guild_slug, *present))
    apply_pending_links()
    return events


def store_profile(found: dict) -> Optional[dict]:
    event = _store(found, source="profile")
    apply_pending_links()
    return event


def parse_known_recipes(data: Optional[dict]) -> List[int]:
    """Every recipe ID the character knows, in all professions."""
    found = set()
    for kind in ("primaries", "secondaries"):
        for p in (data or {}).get(kind) or []:
            for tier in p.get("tiers") or []:
                found.update(r["id"] for r in tier.get("known_recipes") or [] if isinstance(r.get("id"), int))
    return sorted(found)


def store_professions(character_slug: str, data: Optional[dict]):
    """Keeps the primary professions and every known recipe from Blizzard's
    professions document."""
    found = [{"id": p["id"], "name": p["name"], "skill": p["skill"], "max": p["max"], "primary": p["primary"]}
             for p in gear.parse_professions(data)]
    game, region = _scope()
    db.execute("UPDATE roster_characters SET professions = ?, known_recipes = ? "
               "WHERE game = ? AND region = ? AND slug = ?",
               (json.dumps(found), json.dumps(parse_known_recipes(data)), game, region, character_slug))


def known_recipes(c: dict) -> List[int]:
    """A stored character's known recipe IDs."""
    try:
        return json.loads(c.get("known_recipes") or "[]")
    except ValueError:
        return []


def _profile_missing(character_slug: str):
    game, region = _scope()
    db.execute("UPDATE roster_characters SET profile_found = 0, profile_checked_at = ? "
               "WHERE game = ? AND region = ? AND slug = ?", (timestamp(), game, region, character_slug))


def due_for_profile_check(now: Optional[datetime] = None) -> List[dict]:
    """Characters whose profile is worth checking now: level 10 or higher
    (or linked, so a fresh link is filled in), recently active ones every
    time, idle ones every few hours."""
    now = now or datetime.now(timezone.utc)
    active_since = (now - timedelta(days=ACTIVE_DAYS)).timestamp() * 1000
    idle_before = (now - timedelta(hours=IDLE_CHECK_HOURS)).strftime(TIME_FORMAT)
    due = []
    for c in characters():
        if (c["level"] or 0) < PROFILE_MIN_LEVEL and not c["discord_id"]:
            continue
        active = c["last_login"] is None or c["last_login"] >= active_since
        if active or not c["profile_checked_at"] or c["profile_checked_at"] <= idle_before:
            due.append(c)
    return due


# --- talking to Blizzard -----------------------------------------------------------------------------

async def sync_guild(bot) -> Optional[dict]:
    """Reads the configured guild's roster. None when no guild is set."""
    name = (config.GUILD_ROSTER_GUILD or "").strip()
    if not name:
        return None
    members = await wow_api.guild_roster(bot, name)
    if members is None:
        raise RosterError(f"Blizzard doesn't know a guild called {name} in this region. Check the name in Settings.")
    events = await asyncio.to_thread(store_roster, members, wow_api.slug(name))
    return {"members": len(members), "events": events}


async def refresh_profiles(bot, rows: Optional[List[dict]] = None) -> dict:
    """Checks the profiles of the characters that are due. Returns the
    level events and how many were checked."""
    rows = due_for_profile_check() if rows is None else rows
    events, checked, failed = [], 0, 0
    gate = asyncio.Semaphore(PROFILE_CONCURRENCY)

    async def check(c, session):
        nonlocal checked, failed
        async with gate:
            try:
                found = await wow_api.character_profile(bot, c["slug"], session=session)
            except (wow_api.WowApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                failed += 1
                log.debug("Profile check for %s failed: %s", c["slug"], exc)
                return
            checked += 1
            if found is None:
                await asyncio.to_thread(_profile_missing, c["slug"])
                return
            event = await asyncio.to_thread(store_profile, found)
            if event:
                events.append(event)
            # Professions change only in game, so they're read again only after a logout.
            if (c.get("professions") is None or c.get("known_recipes") is None
                    or found.get("last_login") != c.get("last_login")):
                try:
                    data = await wow_api.character_professions(bot, c["slug"], session=session)
                except (wow_api.WowApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.debug("Professions of %s not read: %s", c["slug"], exc)
                else:
                    await asyncio.to_thread(store_professions, c["slug"], data)

    if rows:
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
            await asyncio.gather(*(check(c, session) for c in rows))
    if failed:
        log.warning("Couldn't check %d of %d character profiles on Blizzard's API", failed, len(rows))
    return {"checked": checked, "failed": failed, "events": events}


async def find_or_add(bot, text: str) -> dict:
    """A character by "First Surname" (or its slug): from the roster, or
    looked up on Blizzard and added. RosterError when it can't be found."""
    character_slug = wow_api.slug(text)
    if not character_slug:
        raise RosterError("Type the character's name, e.g. Eggmike Thewhite.")
    known = await asyncio.to_thread(get, character_slug)
    if known:
        return known
    found = await wow_api.character_profile(bot, character_slug)
    if found is None:
        raise RosterError(f"Couldn't find {text.strip()} on Blizzard. Check the spelling (first name and surname). "
                          f"Characters can be found from level {PROFILE_MIN_LEVEL}, after logging out once, or "
                          f"once they're in the guild.")
    await asyncio.to_thread(store_profile, found)
    return await asyncio.to_thread(get, found["slug"])


# --- linking ---------------------------------------------------------------------------------------------

def link(character_slug: str, discord_id: int, *, by: str, as_moderator: bool) -> dict:
    c = get(character_slug)
    if c is None:
        raise RosterError("That character isn't on the roster.")
    if c["discord_id"] and c["discord_id"] != int(discord_id) and not as_moderator:
        raise RosterError(f"{display_name(c)} is already linked to someone else. Ask a moderator if it's yours.")
    game, region = _scope()
    has_main = db.query("SELECT 1 FROM roster_characters WHERE game = ? AND region = ? AND discord_id = ? "
                        "AND is_main = 1 AND character_id != ?", (game, region, int(discord_id), c["character_id"]))
    db.execute("UPDATE roster_characters SET discord_id = ?, is_main = ?, linked_by = ?, linked_at = ?, updated_at = ? "
               "WHERE game = ? AND region = ? AND character_id = ?",
               (int(discord_id), 0 if has_main else 1, by, timestamp(), timestamp(), game, region, c["character_id"]))
    return get(character_slug)


def remember_link(character_slug: str, discord_id: int, *, by: str, main_spec_id: Optional[int] = None,
                  off_spec_id: Optional[int] = None):
    """Links the character to the member as soon as it shows up on the
    roster (apply_pending_links). Replaces an earlier pending link for it."""
    game, region = _scope()
    db.execute("INSERT OR REPLACE INTO roster_pending_links (game, region, slug, discord_id, main_spec_id, off_spec_id, "
               "linked_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (game, region, character_slug, int(discord_id), main_spec_id, off_spec_id, by, timestamp()))


def forget_link(character_slug: str):
    game, region = _scope()
    db.execute("DELETE FROM roster_pending_links WHERE game = ? AND region = ? AND slug = ?",
               (game, region, character_slug))


def pending_links() -> List[dict]:
    game, region = _scope()
    return [dict(r) for r in db.query("SELECT * FROM roster_pending_links WHERE game = ? AND region = ?",
                                      (game, region))]


def apply_pending_links() -> List[dict]:
    """Links waiting characters that are on the roster now. A character
    someone else linked in the meantime keeps that link. Returns the
    characters that were linked."""
    linked = []
    for pending in pending_links():
        c = get(pending["slug"])
        if c is None:
            continue
        forget_link(pending["slug"])
        if c["discord_id"] and c["discord_id"] != pending["discord_id"]:
            log.info("Pending link of %s to %s dropped: already linked to %s", pending["slug"], pending["discord_id"],
                     c["discord_id"])
            continue
        c = link(pending["slug"], pending["discord_id"], by=pending["linked_by"] or "application approval",
                 as_moderator=False)
        if pending["main_spec_id"] and c["main_spec_id"] is None:
            game, region = _scope()
            db.execute("UPDATE roster_characters SET main_spec_id = ?, off_spec_id = ? "
                       "WHERE game = ? AND region = ? AND character_id = ?",
                       (pending["main_spec_id"], pending["off_spec_id"], game, region, c["character_id"]))
        log.info("Linked %s to %s (waiting since the application was approved)", pending["slug"],
                 pending["discord_id"])
        linked.append(get(pending["slug"]))
    return linked


def unlink(character_slug: str, *, discord_id: int, as_moderator: bool) -> dict:
    c = get(character_slug)
    if c is None or not c["discord_id"]:
        raise RosterError("That character isn't linked to anyone.")
    if c["discord_id"] != int(discord_id) and not as_moderator:
        raise RosterError(f"{display_name(c)} isn't yours to unlink.")
    game, region = _scope()
    with db.transaction() as conn:
        conn.execute("UPDATE roster_characters SET discord_id = NULL, is_main = 0, linked_by = NULL, linked_at = NULL, "
                     "main_spec_id = NULL, off_spec_id = NULL, updated_at = ? "
                     "WHERE game = ? AND region = ? AND character_id = ?",
                     (timestamp(), game, region, c["character_id"]))
        if c["is_main"]:  # the member's next character (highest level) becomes their main
            conn.execute("UPDATE roster_characters SET is_main = 1 WHERE rowid = (SELECT rowid FROM roster_characters "
                         "WHERE game = ? AND region = ? AND discord_id = ? ORDER BY level DESC LIMIT 1)",
                         (game, region, c["discord_id"]))
    return get(character_slug)


def set_main(character_slug: str, *, discord_id: int, as_moderator: bool) -> dict:
    c = get(character_slug)
    if c is None or not c["discord_id"]:
        raise RosterError("Link the character to a member first.")
    if c["discord_id"] != int(discord_id) and not as_moderator:
        raise RosterError(f"{display_name(c)} isn't yours.")
    game, region = _scope()
    with db.transaction() as conn:
        conn.execute("UPDATE roster_characters SET is_main = 0 WHERE game = ? AND region = ? AND discord_id = ?",
                     (game, region, c["discord_id"]))
        conn.execute("UPDATE roster_characters SET is_main = 1, updated_at = ? "
                     "WHERE game = ? AND region = ? AND character_id = ?",
                     (timestamp(), game, region, c["character_id"]))
    return get(character_slug)


def set_alt(character_slug: str, *, discord_id: int, as_moderator: bool) -> dict:
    """Marks a linked character as an alt (not the member's main)."""
    c = get(character_slug)
    if c is None or not c["discord_id"]:
        raise RosterError("Link the character to a member first.")
    if c["discord_id"] != int(discord_id) and not as_moderator:
        raise RosterError(f"{display_name(c)} isn't yours.")
    game, region = _scope()
    db.execute("UPDATE roster_characters SET is_main = 0, updated_at = ? WHERE game = ? AND region = ? "
               "AND character_id = ?", (timestamp(), game, region, c["character_id"]))
    return get(character_slug)


def set_specs(character_slug: str, main_spec_id: Optional[int], off_spec_id: Optional[int], *,
              discord_id: int, as_moderator: bool, class_specs: Dict[int, List[int]]) -> dict:
    """class_specs: class id -> its spec ids (wow_api.reference), so a
    Priest can't be given a Warrior spec."""
    c = get(character_slug)
    if c is None:
        raise RosterError("That character isn't on the roster.")
    if c["discord_id"] != int(discord_id) and not as_moderator:
        raise RosterError(f"{display_name(c)} isn't yours.")
    allowed = class_specs.get(c["class_id"], [])
    for spec in (main_spec_id, off_spec_id):
        if spec is not None and spec not in allowed:
            raise RosterError("Pick specs of the character's own class.")
    game, region = _scope()
    db.execute("UPDATE roster_characters SET main_spec_id = ?, off_spec_id = ?, updated_at = ? "
               "WHERE game = ? AND region = ? AND character_id = ?",
               (main_spec_id, off_spec_id, timestamp(), game, region, c["character_id"]))
    return get(character_slug)


# --- announcements ---------------------------------------------------------------------------------------

def pending_announcements() -> List[dict]:
    """Milestone events not announced yet, oldest first, with their character."""
    return [e for e in reversed(recent_events(200)) if e["milestone"] and not e["announced_at"]]


def should_announce(event: dict, now: Optional[datetime] = None) -> Optional[str]:
    """None to announce, or why not (stored as the event's outcome)."""
    now = now or datetime.now(timezone.utc)
    created = datetime.strptime(event["created_at"], TIME_FORMAT).replace(tzinfo=timezone.utc)
    if now - created > timedelta(hours=ANNOUNCE_WITHIN_HOURS):
        return "too old"
    if event.get("discord_id") and not event.get("is_main") and not config.LEVELING_ANNOUNCE_ALTS:
        return "alt"
    return None


def mark_announced(event_id: int, outcome: str):
    db.execute("UPDATE roster_events SET announced_at = ?, outcome = ? WHERE id = ?", (timestamp(), outcome, event_id))
