"""
Auction house prices: what an item costs on the guild's auction house,
from Blizzard's snapshots. Shared by cogs/auction_prices.py (keeps the
prices fresh, answers "price [Item]" in the guild chat channel) and
webapi/auction_api.py (the dashboard's Auction prices page).

WoW Forever has no realms: auction houses hang off a ruleset (PvP, Normal,
...), and every ruleset has several houses per faction, all with the same
name (docs/wow-forever-api.md). Which one the guild uses is a setting
(AUCTION_RULESET_ID, AUCTION_HOUSE_ID), picked on the dashboard, where
moderators can compare houses by what's for sale.

A house is two documents: commodities (stackable goods with a price per
unit) and everything else (a buyout for the whole stack). Blizzard renews
them about hourly; the bot asks with If-Modified-Since, so an unchanged
snapshot costs a 304 and nothing more. Per item it keeps the cheapest and
the median price per unit (copper) and how many are for sale; items with
random suffixes count as their base item.

Item names come from Blizzard's item search (whole words; a word ending in
"*" matches its beginning), cached for a while.
"""

import asyncio
import email.utils
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import aiohttp

import config
import db
import wow_api

log = logging.getLogger("wow-apply-bot.auction_prices")

KINDS = ("commodities", "items")
SEARCH_TTL_SECONDS = 6 * 3600
SEARCH_CACHE_SIZE = 500
SEARCH_PAGE_SIZE = 100
HOUSES_TTL_SECONDS = 3600
MAX_ITEMS_PER_MESSAGE = 3
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FILLER_WORDS = ("of", "on")  # "price of [X]"; not "for", so the bot's own reply never reads as a question


class AuctionError(Exception):
    """Prices aren't available; the message says why and what to do."""


# --- which auction house ---------------------------------------------------------------------------

def house_key(ruleset_id: Optional[int] = None, house_id: Optional[int] = None) -> Optional[str]:
    """'forever:eu:1:113' for the configured (or given) house, None when none is picked."""
    ruleset_id = int(ruleset_id if ruleset_id is not None else config.AUCTION_RULESET_ID or 0)
    house_id = int(house_id if house_id is not None else config.AUCTION_HOUSE_ID or 0)
    if not ruleset_id or not house_id:
        return None
    return f"{config.GAME_VERSION}:{wow_api.region()}:{ruleset_id}:{house_id}"


_houses: Dict[tuple, Tuple[float, List[dict]]] = {}


async def houses(bot, ruleset_id: int) -> List[dict]:
    """[{id, name}] of a ruleset's auction houses (cached for an hour)."""
    key = (wow_api.namespace("dynamic"), ruleset_id)
    cached = _houses.get(key)
    if cached and time.monotonic() - cached[0] < HOUSES_TTL_SECONDS:
        return cached[1]
    status, _headers, body = await wow_api._client(bot).request(
        f"/data/wow/ruleset/{int(ruleset_id)}/auctions/index", wow_api.namespace("dynamic"),
        locale=config.BLIZZARD_LOCALE)
    if status == 404:
        raise AuctionError("Blizzard has no auction houses for that ruleset.")
    if status != 200:
        raise AuctionError(f"Blizzard answered {status} for the auction houses. Try again in a minute.")
    found = [{"id": h["id"], "name": wow_api._name(h.get("name")) or f"Auction house {h['id']}"}
             for h in (body or {}).get("auctions", []) if "id" in h]
    _houses[key] = (time.monotonic(), found)
    return found


# --- prices from a snapshot ------------------------------------------------------------------------

def unit_price(auction: dict) -> Optional[float]:
    """Copper per item: commodities have unit_price, other auctions a buyout for the stack (None: bid only)."""
    if auction.get("unit_price"):
        return float(auction["unit_price"])
    if auction.get("buyout"):
        return auction["buyout"] / max(1, int(auction.get("quantity") or 1))
    return None


def collect(auctions: Iterable[dict], into: Dict[int, List[Tuple[float, int]]]) -> int:
    """Adds each auction's (price per unit, quantity) under its item; returns how many were read."""
    count = 0
    for auction in auctions:
        item_id = (auction.get("item") or {}).get("id")
        price = unit_price(auction)
        if not item_id or price is None:
            continue
        into.setdefault(int(item_id), []).append((price, max(1, int(auction.get("quantity") or 1))))
        count += 1
    return count


def summarise(listings: List[Tuple[float, int]]) -> dict:
    """{min_price, median_price, quantity, listings}: the median is the middle unit for sale, not the middle listing."""
    rows = sorted(listings)
    total = sum(q for _, q in rows)
    half, running, median = (total + 1) / 2, 0, rows[-1][0]
    for price, quantity in rows:
        running += quantity
        if running >= half:
            median = price
            break
    return {"min_price": int(round(rows[0][0])), "median_price": int(round(median)), "quantity": total,
            "listings": len(rows)}


def _http_time(value: Optional[str]) -> Optional[str]:
    """Last-Modified ('Mon, 9 Nov 2026 12:12:15 GMT') -> '2026-11-09T12:12:15Z'."""
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc).strftime(TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def snapshot(key: Optional[str] = None) -> Optional[dict]:
    """{snapshot_at, checked_at, auctions, items} of the house's stored snapshot, None before the first."""
    key = key or house_key()
    if key is None:
        return None
    rows = db.query("SELECT * FROM auction_snapshots WHERE house_key = ?", (key,))
    if not rows:
        return None
    row = dict(rows[0])
    return {"snapshot_at": row["snapshot_at"], "checked_at": row["checked_at"], "auctions": row["auctions"],
            "items": row["items"], "modified": json.loads(row["modified"] or "{}")}


def _store(key: str, prices: Dict[int, dict], snapshot_at: Optional[str], modified: dict, auctions: int):
    with db.transaction() as conn:
        conn.execute("DELETE FROM auction_prices WHERE house_key = ?", (key,))
        conn.executemany(
            "INSERT INTO auction_prices (house_key, item_id, min_price, median_price, quantity, listings) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(key, item_id, p["min_price"], p["median_price"], p["quantity"], p["listings"])
             for item_id, p in prices.items()])
        conn.execute(
            "INSERT INTO auction_snapshots (house_key, snapshot_at, modified, checked_at, auctions, items) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(house_key) DO UPDATE SET snapshot_at = excluded.snapshot_at, "
            "modified = excluded.modified, checked_at = excluded.checked_at, auctions = excluded.auctions, "
            "items = excluded.items",
            (key, snapshot_at, json.dumps(modified), _now(), auctions, len(prices)))


def _touch(key: str):
    db.execute("UPDATE auction_snapshots SET checked_at = ? WHERE house_key = ?", (_now(), key))


def _path(ruleset_id: int, house_id: int, kind: str) -> str:
    base = f"/data/wow/ruleset/{int(ruleset_id)}/auctions/{int(house_id)}"
    return f"{base}/commodities" if kind == "commodities" else base


async def refresh(bot, *, force: bool = False) -> dict:
    """Reads the configured house's snapshot from Blizzard when it's newer
    than the stored one (or `force`). Returns {"changed", "snapshot_at",
    "items", "auctions"}; raises AuctionError when it can't."""
    key = house_key()
    if key is None:
        raise AuctionError("Pick your auction house first (Auction prices page).")
    ruleset_id, house_id = int(config.AUCTION_RULESET_ID), int(config.AUCTION_HOUSE_ID)
    client = wow_api._client(bot)
    stored = await asyncio.to_thread(snapshot, key)
    known = {} if force or stored is None else stored["modified"]
    namespace = wow_api.namespace("dynamic")

    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=120)) as session:
        async def get(kind: str, since: Optional[str]):
            headers = {"If-Modified-Since": since} if since else None
            status, response_headers, body = await client.request(
                _path(ruleset_id, house_id, kind), namespace, session=session, locale=config.BLIZZARD_LOCALE,
                headers=headers)
            if status not in (200, 304):
                raise AuctionError(f"Blizzard answered {status} for the auction house. Try again later.")
            return status, response_headers.get("Last-Modified"), body

        answers = {kind: await get(kind, known.get(kind)) for kind in KINDS}
        if all(status == 304 for status, _m, _b in answers.values()):
            await asyncio.to_thread(_touch, key)
            return {"changed": False, "snapshot_at": stored["snapshot_at"], "items": stored["items"],
                    "auctions": stored["auctions"]}
        listings: Dict[int, List[Tuple[float, int]]] = {}
        modified, auctions = {}, 0
        for kind in KINDS:
            status, last_modified, body = answers.pop(kind)
            if status == 304:  # the other half changed: this half is needed again in full
                status, last_modified, body = await get(kind, None)
            auctions += collect((body or {}).get("auctions") or [], listings)
            modified[kind] = last_modified
            del body
    prices = {item_id: summarise(rows) for item_id, rows in listings.items()}
    times = [t for t in (_http_time(m) for m in modified.values()) if t]
    snapshot_at = max(times) if times else _now()
    await asyncio.to_thread(_store, key, prices, snapshot_at, modified, auctions)
    log.info("Auction house %s: %d auctions of %d items (snapshot %s)", key, auctions, len(prices), snapshot_at)
    return {"changed": True, "snapshot_at": snapshot_at, "items": len(prices), "auctions": auctions}


def prices(item_ids: Iterable[int], key: Optional[str] = None) -> Dict[int, dict]:
    """{item_id: {min_price, median_price, quantity, listings}} of the items on sale."""
    key = key or house_key()
    ids = sorted({int(i) for i in item_ids})
    if key is None or not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT * FROM auction_prices WHERE house_key = ? AND item_id IN ({marks})", (key, *ids))
    return {r["item_id"]: {k: r[k] for k in ("min_price", "median_price", "quantity", "listings")} for r in rows}


async def compare(bot, ruleset_id: int, house_ids: List[int], item_id: int) -> List[dict]:
    """How much of an item each house has for sale right now ([{house_id, quantity, listings, min_price}]),
    to find out which house is the guild's. Reads each house, so it's slow: a setup tool."""
    client = wow_api._client(bot)
    out = []
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=120)) as session:
        for house_id in house_ids:
            listings: Dict[int, List[Tuple[float, int]]] = {}
            for kind in KINDS:
                status, _headers, body = await client.request(_path(ruleset_id, house_id, kind),
                                                              wow_api.namespace("dynamic"), session=session,
                                                              locale=config.BLIZZARD_LOCALE)
                if status == 200:
                    collect([a for a in (body or {}).get("auctions") or []
                             if (a.get("item") or {}).get("id") == item_id], listings)
                del body
            rows = listings.get(item_id)
            out.append({"house_id": house_id, **(summarise(rows) if rows else
                                                 {"min_price": None, "median_price": None, "quantity": 0,
                                                  "listings": 0})})
    return out


# --- finding items by name -------------------------------------------------------------------------

_search: Dict[tuple, Tuple[float, List[dict]]] = {}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("’", "'")).strip()


async def search_items(bot, text: str) -> List[dict]:
    """[{id, name, quality}] of items whose name has these words (the last
    may be unfinished), from Blizzard's item search; cached."""
    words = _normalise(text)
    if len(words) < 2:
        return []
    key = (wow_api.namespace("static"), config.BLIZZARD_LOCALE, words.lower())
    cached = _search.get(key)
    if cached and time.monotonic() - cached[0] < SEARCH_TTL_SECONDS:
        return cached[1]
    client = wow_api._client(bot)
    found: Dict[int, dict] = {}
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
        for query in (words, words if words.endswith("*") else f"{words}*"):
            status, _headers, body = await client.request(
                "/data/wow/search/item", wow_api.namespace("static"),
                {f"name.{config.BLIZZARD_LOCALE}": query, "_pageSize": SEARCH_PAGE_SIZE}, session=session,
                locale=config.BLIZZARD_LOCALE)
            if status != 200:
                raise AuctionError(f"Blizzard's item search answered {status}. Try again in a minute.")
            for result in (body or {}).get("results") or []:
                data = result.get("data") or {}
                if data.get("id") and data["id"] not in found:
                    found[data["id"]] = {"id": data["id"], "name": wow_api._name(data.get("name")),
                                         "quality": (data.get("quality") or {}).get("type")}
            if found:
                break
    items = list(found.values())
    if len(_search) >= SEARCH_CACHE_SIZE:
        _search.clear()
    _search[key] = (time.monotonic(), items)
    return items


def rank(items: List[dict], text: str, priced: Dict[int, dict]) -> List[dict]:
    """Exact name first, then names starting with the text, then items on sale (most for sale first)."""
    needle = _normalise(text).lower().rstrip("*")

    def key(item):
        name = (item.get("name") or "").lower()
        price = priced.get(item["id"])
        return (name != needle, not name.startswith(needle), price is None, -(price or {}).get("quantity", 0), name)

    return sorted(items, key=key)


async def lookup(bot, text: str, limit: int = 25) -> List[dict]:
    """Items matching the text with their price ({..., "price": {...} | None}), best match first."""
    items = await search_items(bot, text)
    priced = await asyncio.to_thread(prices, [i["id"] for i in items])
    return [{**item, "price": priced.get(item["id"])} for item in rank(items, text, priced)[:limit]]


async def exact(bot, name: str) -> Optional[dict]:
    """The item with exactly this name (any case) and its price; with several, the one most on sale.
    A linked crafting recipe ('Blacksmithing: Officer's Belt') counts as the item it makes."""
    results = await lookup(bot, name, limit=SEARCH_PAGE_SIZE)
    wanted = _normalise(name).lower()
    same = [r for r in results if (r.get("name") or "").lower() == wanted]
    if same:
        return same[0]
    profession, colon, made = name.partition(": ")
    if colon and made.strip() and " " not in profession.strip():
        return await exact(bot, made)
    return None


# --- words ---------------------------------------------------------------------------------------

def money(copper: Optional[int], *, compact: bool = False) -> str:
    """4g 33s 22c (compact: 4g33s22c); units that are 0 are left out, except a plain 0c."""
    copper = int(copper or 0)
    parts = [f"{v}{u}" for v, u in ((copper // 10000, "g"), (copper // 100 % 100, "s"), (copper % 100, "c")) if v]
    return ("" if compact else " ").join(parts) or "0c"


def age_text(when: Optional[str], now: Optional[float] = None) -> str:
    """'just now', '12mins ago', '3hrs ago', '2 days ago' for a stored UTC time."""
    if not when:
        return "unknown age"
    then = datetime.strptime(when, TIME_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    seconds = max(0, int((now if now is not None else time.time()) - then))
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}min{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}hr{'s' if hours != 1 else ''} ago"
    return f"{hours // 24} days ago"


_ITEMS = r"((?:\[[^\[\]\n]{1,80}\](?:\([^)\s]*\))?[\s,&]*(?:and\s+)?)+)"


def requested_items(text: str, triggers: Iterable[str], *, after: bool = False) -> List[str]:
    """The [Item]s someone asks the price of, e.g. 'price [Linen Cloth]',
    'price check [A][B]', 'what is the price of [A]?' (at most a few). The
    phrase must come right before the first item; with `after`, right after
    the last one also counts ('[Iron Bar] price?')."""
    phrases = sorted({_normalise(t).lower() for t in triggers if _normalise(t)}, key=len, reverse=True)
    if not phrases or "[" not in (text or ""):
        return []
    alternatives = "|".join(r"\s+".join(re.escape(w) for w in p.split(" ")) for p in phrases)
    fillers = "|".join(FILLER_WORDS)
    text = _normalise(text)
    match = re.search(rf"(?:^|[\s:>,.!?(-])(?:{alternatives})\s*(?:(?:{fillers})\s+)?[:-]?\s*{_ITEMS}", text,
                      re.IGNORECASE)
    if match is None and after:
        match = re.search(rf"{_ITEMS}[:-]?\s*(?:{alternatives})\s*[?!.]*\s*$", text, re.IGNORECASE)
    if not match:
        return []
    names: List[str] = []
    for name in re.findall(r"\[([^\[\]\n]{1,80})\]", match.group(1)):
        name = name.strip()
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return names[:MAX_ITEMS_PER_MESSAGE]
