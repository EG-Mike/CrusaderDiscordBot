"""
Statistics on the application card (and /lookup and the dashboard): counts
from a character's achievement statistics, e.g. raid boss kills, which
moderators pick on the dashboard (Applications, Card statistics).

Blizzard's character statistics (/profile/wow/character/{slug}/achievements/
statistics) are categories ("Dungeons & Raids" > "Boss Kills", "Deaths",
...) of {id, name, quantity}. Boss kills are named "<boss> kills
(<instance>)", so they're grouped per instance on the card, one line each.

Blizzard only lists statistics a character has (a boss never killed isn't
there), and has no list of every statistic. So the statistics to choose
from are the ones the bot has seen: every character it reads (gear.fetch:
applications, Refresh, /lookup) adds what it has to `card_stats_seen`, and
"Scan the guild" reads the guild's members. A chosen statistic a character
doesn't have counts as 0.

Statistics are game data, so the catalog and the choice are kept per game
version.
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional

import aiohttp

import config
import db
import wow_api

log = logging.getLogger("wow-apply-bot.card_stats")

SCAN_CONCURRENCY = 4
_KILLS = re.compile(r"^(?P<boss>.+?) kills \((?P<instance>[^)]+)\)$", re.IGNORECASE)


def _game() -> str:
    return config.GAME_VERSION


# --- Blizzard's statistics document -------------------------------------------------------------

def parse(data: Optional[dict]) -> List[dict]:
    """Every statistic in the document: [{id, name, category, quantity}],
    category as "Dungeons & Raids > Boss Kills"."""
    found: List[dict] = []

    def walk(category: dict, path: List[str]):
        for stat in category.get("statistics") or []:
            if isinstance(stat.get("id"), int) and stat.get("name"):
                found.append({"id": stat["id"], "name": wow_api._name(stat["name"]), "category": " > ".join(path),
                              "quantity": stat.get("quantity")})
        for sub in category.get("sub_categories") or []:
            walk(sub, path + [wow_api._name(sub.get("name")) or "Other"])

    for category in (data or {}).get("categories") or []:
        walk(category, [wow_api._name(category.get("name")) or "Other"])
    return found


def values(entries: List[dict]) -> Dict[str, float]:
    """{statistic id (text, for JSON): quantity}, as stored with a character summary."""
    return {str(e["id"]): e["quantity"] for e in entries if isinstance(e.get("quantity"), (int, float))}


def split_name(name: str) -> dict:
    """{"label", "group"}: "Moroes kills (Karazhan)" -> Moroes / Karazhan;
    other statistics keep their name and have no group."""
    match = _KILLS.match(name or "")
    if match:
        return {"label": match["boss"].strip(), "group": match["instance"].strip()}
    return {"label": name or "?", "group": None}


# --- what the bot has seen, and what moderators chose -----------------------------------------------

def record_seen(entries: List[dict]):
    """Adds the statistics a character has to the catalog (names refreshed).
    Never raises: the catalog is a convenience."""
    if not entries:
        return
    try:
        with db.transaction() as conn:
            conn.executemany(
                "INSERT INTO card_stats_seen (game, stat_id, name, category) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(game, stat_id) DO UPDATE SET name = excluded.name, category = excluded.category",
                [(_game(), e["id"], e["name"], e["category"]) for e in entries])
    except Exception:
        log.warning("Couldn't record the statistics seen", exc_info=True)


def catalog() -> List[dict]:
    """Every statistic seen: [{id, name, category, label, group, chosen}], by category and name."""
    chosen_ids = {c["id"] for c in chosen()}
    rows = db.query("SELECT stat_id, name, category FROM card_stats_seen WHERE game = ? ORDER BY category, name",
                    (_game(),))
    return [{"id": r["stat_id"], "name": r["name"], "category": r["category"], **split_name(r["name"]),
             "chosen": r["stat_id"] in chosen_ids} for r in rows]


def chosen() -> List[dict]:
    """The statistics on the card, in order: [{id, name, label, group}]."""
    if not db.exists():
        return []
    rows = db.query("SELECT c.stat_id, s.name FROM card_stats_chosen c LEFT JOIN card_stats_seen s "
                    "ON s.game = c.game AND s.stat_id = c.stat_id WHERE c.game = ? ORDER BY c.position",
                    (_game(),))
    return [{"id": r["stat_id"], "name": r["name"] or f"Statistic {r['stat_id']}", **split_name(r["name"] or "")}
            for r in rows]


def set_chosen(stat_ids: List[int]):
    """The statistics on the card, in this order (only ones the bot has seen)."""
    known = {r["stat_id"] for r in db.query("SELECT stat_id FROM card_stats_seen WHERE game = ?", (_game(),))}
    unknown = [i for i in stat_ids if i not in known]
    if unknown:
        raise ValueError(f"Unknown statistic(s): {', '.join(map(str, unknown))}. Reload the page and try again.")
    with db.transaction() as conn:
        conn.execute("DELETE FROM card_stats_chosen WHERE game = ?", (_game(),))
        conn.executemany("INSERT INTO card_stats_chosen (game, stat_id, position) VALUES (?, ?, ?)",
                         [(_game(), stat_id, i) for i, stat_id in enumerate(dict.fromkeys(stat_ids))])


async def scan(bot, characters: List[dict]) -> dict:
    """Reads the statistics of these characters (roster rows with a slug)
    to fill the catalog. {"read", "failed", "statistics"}."""
    client = wow_api._client(bot)
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)
    read = failed = 0

    async def one(c, session):
        nonlocal read, failed
        async with gate:
            try:
                status, _headers, body = await client.request(
                    f"/profile/wow/character/{c['slug']}/achievements/statistics", wow_api.namespace("profile"),
                    session=session, locale=config.BLIZZARD_LOCALE)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                failed += 1
                return
        if status == 200:
            read += 1
            await asyncio.to_thread(record_seen, parse(body))

    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
        await asyncio.gather(*(one(c, session) for c in characters if c.get("slug")))
    total = db.query("SELECT COUNT(*) AS n FROM card_stats_seen WHERE game = ?", (_game(),))[0]["n"]
    return {"read": read, "failed": failed, "statistics": total}


# --- on the card ---------------------------------------------------------------------------------

def lines(stats: Optional[Dict[str, float]], picks: Optional[List[dict]] = None) -> List[dict]:
    """The chosen statistics for one character, grouped as on the card:
    [{"group": "Karazhan" | None, "items": [{"label", "value"}]}]. A
    statistic the character doesn't have is 0."""
    picks = chosen() if picks is None else picks
    groups: List[dict] = []
    for pick in picks:
        value = (stats or {}).get(str(pick["id"])) or 0
        if groups and groups[-1]["group"] == pick["group"]:  # ungrouped ones in a row share a line too
            groups[-1]["items"].append({"label": pick["label"], "value": value})
        else:
            groups.append({"group": pick["group"], "items": [{"label": pick["label"], "value": value}]})
    return groups


def card_text(stats: Optional[Dict[str, float]], picks: Optional[List[dict]] = None) -> Optional[str]:
    """The card's statistics section, or None when none are chosen. One
    line per instance: '**Karazhan** Attumen `3` · Moroes `3`'; kills of 0
    are dimmed by leaving out the code style."""
    groups = lines(stats, picks)
    if not groups:
        return None
    rows = []
    for g in groups:
        items = " · ".join(f"{i['label']} `{i['value']:g}`" if i["value"] else f"{i['label']} 0" for i in g["items"])
        rows.append(f"**{g['group']}** {items}" if g["group"] else items)
    title = (config.APPLICATION_STATISTICS_TITLE or "").strip()
    return (f"### {title}\n" if title else "") + "\n".join(rows)
