"""
Rank sync: Discord roles against in-game guild ranks. Shared by
cogs/rank_sync.py (keeps the list posted in RANK_SYNC_CHANNEL_ID) and
webapi/rank_sync_api.py (the dashboard's Rank sync page).

Moderators set rules: members with a Discord role (e.g. Fresh) should
have at least an in-game rank (e.g. the guild's Fresh rank). Each member
with a rule's role is matched to their claimed character (their main, or
else a claimed character in the guild; roster.py) and lands in one group:

  promote       in the guild, but below the rank: promote them in the game
  ok            at that rank or higher
  unknown_rank  in the guild, but Blizzard hasn't said their rank yet
  not_in_guild  claimed a character that isn't in the guild (yet)
  no_character  hasn't claimed a character

Blizzard's guild roster gives ranks as numbers only (0 = guild master, a
higher number is a lower rank), so the names are set on the dashboard too.
Ranks change in the game first and reach the bot with the next roster
sync (Blizzard's roster can be up to 3 hours old).
"""

import logging
from typing import Dict, Iterable, List, Optional

import config
import db
import roster
import wow_api

log = logging.getLogger("wow-apply-bot.rank_sync")

STATUSES = ("promote", "unknown_rank", "not_in_guild", "no_character", "ok")
MAX_RANK = 9  # WoW guilds have ranks 0 (guild master) to 9
MAX_RANK_NAME = 40


class RankSyncError(Exception):
    """Something the person can fix; the message says what."""


def guild_slug() -> Optional[str]:
    """The guild's slug (GUILD_ROSTER_GUILD), None when no guild is set."""
    return wow_api.slug(config.GUILD_ROSTER_GUILD or "") or None


def _scope() -> tuple:
    slug = guild_slug()
    if slug is None:
        raise RankSyncError("Set your guild first (Settings, Guild roster).")
    return config.GAME_VERSION, wow_api.region(), slug


# --- rank names ------------------------------------------------------------------------------------

def rank_names() -> Dict[int, str]:
    if guild_slug() is None:
        return {}
    rows = db.query("SELECT rank, name FROM rank_sync_ranks WHERE game = ? AND region = ? AND guild_slug = ?",
                    _scope())
    return {r["rank"]: r["name"] for r in rows}


def rank_label(rank: Optional[int], names: Optional[Dict[int, str]] = None) -> str:
    """'Fresh (rank 5)', 'Guild master', 'Rank 5' or 'Unknown rank'."""
    if rank is None:
        return "Unknown rank"
    name = (names if names is not None else rank_names()).get(rank)
    if name:
        return name
    return "Guild master" if rank == 0 else f"Rank {rank}"


def _check_rank(rank) -> int:
    if not isinstance(rank, int) or isinstance(rank, bool) or not 0 <= rank <= MAX_RANK:
        raise RankSyncError(f"Ranks go from 0 (guild master) to {MAX_RANK}.")
    return rank


def set_rank_names(names: Dict[int, str]):
    """Replaces the rank names ({rank: name}; an empty name removes it)."""
    scope = _scope()
    cleaned = {}
    for rank, name in names.items():
        _check_rank(rank)
        name = (name or "").strip()
        if len(name) > MAX_RANK_NAME:
            raise RankSyncError(f"Rank names are at most {MAX_RANK_NAME} characters.")
        if name:
            cleaned[rank] = name
    with db.transaction() as conn:
        conn.execute("DELETE FROM rank_sync_ranks WHERE game = ? AND region = ? AND guild_slug = ?", scope)
        conn.executemany("INSERT INTO rank_sync_ranks (game, region, guild_slug, rank, name) VALUES (?, ?, ?, ?, ?)",
                         [(*scope, rank, name) for rank, name in sorted(cleaned.items())])


def ranks_seen(characters: Iterable[dict]) -> List[dict]:
    """[{rank, count, examples}] of the guild's ranks on the roster, highest first, to help name them."""
    slug, found = guild_slug(), {}
    for c in characters:
        if c["guild_slug"] == slug and c["guild_rank"] is not None:
            found.setdefault(c["guild_rank"], []).append(c)
    return [{"rank": rank, "count": len(rows),
             "examples": [roster.display_name(c) for c in sorted(rows, key=lambda c: -(c["level"] or 0))[:3]]}
            for rank, rows in sorted(found.items())]


# --- rules ---------------------------------------------------------------------------------------

def rules() -> List[dict]:
    """[{role_id, rank}]: members with the role should have at least that in-game rank."""
    if guild_slug() is None:
        return []
    rows = db.query("SELECT role_id, rank FROM rank_sync_rules WHERE game = ? AND region = ? AND guild_slug = ? "
                    "ORDER BY rank, role_id", _scope())
    return [{"role_id": r["role_id"], "rank": r["rank"]} for r in rows]


def set_rule(role_id: int, rank: int, *, by: str):
    _check_rank(rank)
    db.execute("INSERT INTO rank_sync_rules (game, region, guild_slug, role_id, rank, added_by) VALUES (?, ?, ?, ?, ?, ?) "
               "ON CONFLICT(game, region, guild_slug, role_id) DO UPDATE SET rank = excluded.rank, "
               "added_by = excluded.added_by", (*_scope(), int(role_id), rank, by))


def remove_rule(role_id: int):
    cursor = db.execute("DELETE FROM rank_sync_rules WHERE game = ? AND region = ? AND guild_slug = ? AND role_id = ?",
                        (*_scope(), int(role_id)))
    if not cursor.rowcount:
        raise RankSyncError("There's no rule for that role.")


# --- who should be promoted ----------------------------------------------------------------------

def _character_for(own: List[dict], slug: str) -> Optional[dict]:
    """The member's character to judge: their main in the guild, else their highest claimed character in
    the guild, else their main, else their highest claimed character (`own` is highest level first)."""
    ordered = sorted(own, key=lambda c: (c["guild_slug"] != slug, not c["is_main"]))  # stable: keeps level order
    return ordered[0] if ordered else None


def check(members: Iterable[dict], characters: Iterable[dict], rule_list: List[dict]) -> List[dict]:
    """Every member with a rule's role, with their character and group.

    members: [{id, role_ids, ...}] (extra keys are kept); characters:
    roster rows; rule_list: rules(). A member with several rules' roles is
    held to the highest rank among them (the lowest number)."""
    slug = guild_slug()
    targets = {r["role_id"]: r["rank"] for r in rule_list}
    by_member: Dict[int, List[dict]] = {}
    for c in characters:
        if c["discord_id"]:
            by_member.setdefault(int(c["discord_id"]), []).append(c)
    found = []
    for member in members:
        matched = [(targets[r], r) for r in member["role_ids"] if r in targets]
        if not matched:
            continue
        target, role_id = min(matched)
        character = _character_for(by_member.get(int(member["id"]), []), slug)
        if character is None:
            status = "no_character"
        elif character["guild_slug"] != slug:
            status = "not_in_guild"
        elif character["guild_rank"] is None:
            status = "unknown_rank"
        else:
            status = "promote" if character["guild_rank"] > target else "ok"
        found.append({**member, "target_rank": target, "role_id": role_id, "character": character, "status": status})
    order = {s: i for i, s in enumerate(STATUSES)}
    return sorted(found, key=lambda e: (order[e["status"]], e["target_rank"],
                                        roster.display_name(e["character"]) if e["character"] else e.get("name", "")))


def discord_members(guild, rule_list: List[dict]) -> List[dict]:
    """[{id, name, avatar_url, role_ids}] of the guild's members (no bots) with a rule's role."""
    role_ids = {r["role_id"] for r in rule_list}
    found = []
    for member in getattr(guild, "members", []) if guild is not None else []:
        if getattr(member, "bot", False):
            continue
        mine = [role.id for role in member.roles if role.id in role_ids]
        if mine:
            found.append({"id": member.id, "name": member.display_name, "avatar_url": member.display_avatar.url,
                          "mention": member.mention, "role_ids": mine})
    return found
