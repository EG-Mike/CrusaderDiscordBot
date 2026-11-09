"""
Crafting: who in the guild can make what. Shared by cogs/crafting.py (the
crafting channel in Discord) and webapi/crafting_api.py (the dashboard's
Crafting page); it holds the rules, they handle Discord and HTTP.

Where it comes from:
  - the characters: the guild roster (roster.py). Crafters are the guild's
    members (or, without a guild set, the characters linked to members);
  - what they know: each character's professions and known recipes, read
    from Blizzard after the player logs out (roster.refresh_profiles);
  - the recipes: Blizzard's static data for the game version: every
    profession's recipes by category (recipe_catalog), and per recipe what
    it makes and the reagents (recipe_details). Cached, since they don't
    change during a game version.

Moderators pick "featured" recipes (e.g. high-value ones) on the
dashboard. The crafting channel lists every profession with its crafters
and each featured recipe with who can make it and what to send them.

Order of the featured recipes within a profession: a moderator's own
order, or else hardest first. Blizzard gives no required skill for a
recipe, but lists each category's recipes from easiest to hardest (as the
game's profession window does), so a recipe's place in its category is
its difficulty (0 easiest, 1 hardest; `difficulty`).
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import aiohttp

import config
import db
import roster
import wow_api

log = logging.getLogger("wow-apply-bot.crafting")

CATALOG_TTL_SECONDS = 6 * 3600


class CraftingError(Exception):
    """Something the person can fix or should know; the message says what."""


def _game() -> str:
    return config.GAME_VERSION


def _name(value) -> str:
    return wow_api._name(value)


# --- Blizzard's recipe data -------------------------------------------------------------------------

_catalog: Dict[tuple, tuple] = {}  # (game, locale, profession id) -> (fetched at, catalog)
_media: Dict[tuple, Optional[str]] = {}  # (kind, id) -> icon image


async def _get(bot, path: str, session=None) -> Optional[dict]:
    """A static game data document, None when Blizzard has nothing there."""
    status, _headers, body = await wow_api._client(bot).request(path, wow_api.namespace("static"), session=session,
                                                                locale=config.BLIZZARD_LOCALE)
    if status == 404:
        return None
    if status != 200:
        raise CraftingError(f"Blizzard answered {status}. Try again in a minute.")
    return body


async def icon_url(bot, kind: str, game_id: int, session=None) -> Optional[str]:
    """The icon image of a profession or recipe (cached for the process)."""
    key = (kind, game_id)
    if key not in _media:
        try:
            media = await _get(bot, f"/data/wow/media/{kind}/{game_id}", session)
        except (CraftingError, aiohttp.ClientError, asyncio.TimeoutError):
            return None  # not cached: tried again next time
        _media[key] = next((a.get("value") for a in (media or {}).get("assets") or [] if a.get("key") == "icon"), None)
    return _media[key]


async def professions(bot) -> List[dict]:
    """[{id, name, icon_url}] of the game version's professions."""
    try:
        ref = await wow_api.reference(bot)
    except wow_api.WowApiError as exc:
        raise CraftingError(str(exc)) from None
    found = ref.get("professions") or []
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
        icons = await asyncio.gather(*(icon_url(bot, "profession", p["id"], session) for p in found))
    return [{**p, "icon_url": url} for p, url in zip(found, icons)]


async def recipe_catalog(bot, profession_id: int) -> dict:
    """{id, name, categories: [{name, recipes: [{id, name}]}]}: every recipe
    of a profession, in Blizzard's categories. Cached for a few hours."""
    key = (_game(), config.BLIZZARD_LOCALE, int(profession_id))
    cached = _catalog.get(key)
    if cached and time.monotonic() - cached[0] < CATALOG_TTL_SECONDS:
        return cached[1]
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            profession = await _get(bot, f"/data/wow/profession/{int(profession_id)}", session)
            if profession is None:
                raise CraftingError("Blizzard doesn't know that profession.")
            tiers = await asyncio.gather(*(
                _get(bot, f"/data/wow/profession/{int(profession_id)}/skill-tier/{t['id']}", session)
                for t in profession.get("skill_tiers") or []))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise CraftingError("Couldn't reach Blizzard. Try again in a minute.") from None
    categories = []
    for tier in tiers:
        for category in (tier or {}).get("categories") or []:
            recipes = [{"id": r["id"], "name": _name(r.get("name"))} for r in category.get("recipes") or []]
            if recipes:
                categories.append({"name": _name(category.get("name")) or "Other", "recipes": recipes})
    catalog = {"id": int(profession_id), "name": _name(profession.get("name")), "categories": categories}
    _catalog[key] = (time.monotonic(), catalog)
    return catalog


def place_in_catalog(catalog: dict, recipe_id: int) -> Optional[dict]:
    """{category, difficulty} of a recipe in a profession's catalog: its
    place in its category, 0 for the first (easiest) to 1 for the last
    (hardest). None when it isn't in the catalog."""
    for category in catalog.get("categories") or []:
        ids = [r["id"] for r in category["recipes"]]
        if recipe_id in ids:
            index = ids.index(recipe_id)
            return {"category": category["name"], "difficulty": index / (len(ids) - 1) if len(ids) > 1 else 1.0}
    return None


def parse_recipe(data: dict) -> dict:
    """What a recipe makes and needs, from Blizzard's recipe document."""
    item = data.get("crafted_item") or data.get("alliance_crafted_item") or data.get("horde_crafted_item") or {}
    return {
        "recipe_id": data.get("id"),
        "name": _name(data.get("name")) or "?",
        "item_id": item.get("id"),
        "item_name": _name(item.get("name")) or None,
        # How many items come out. Only for recipes that make an item: for enchants Blizzard puts the
        # enchant's strength here (e.g. 10 for Enchant Weapon - Revelation), not a count.
        "quantity": (data.get("crafted_quantity") or {}).get("value") if item.get("id") else None,
        "reagents": [{"id": (r.get("reagent") or {}).get("id"), "name": _name((r.get("reagent") or {}).get("name")),
                      "quantity": r.get("quantity")} for r in data.get("reagents") or []],
    }


async def recipe_details(bot, recipe_id: int) -> dict:
    try:
        data = await _get(bot, f"/data/wow/recipe/{int(recipe_id)}")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise CraftingError("Couldn't reach Blizzard. Try again in a minute.") from None
    if data is None:
        raise CraftingError("Blizzard doesn't know that recipe.")
    return {**parse_recipe(data), "icon_url": await icon_url(bot, "recipe", int(recipe_id))}


# --- featured recipes ---------------------------------------------------------------------------------

def _featured_row(row) -> dict:
    found = dict(row)
    found["reagents"] = json.loads(found.get("reagents") or "[]")
    if not found.get("item_id"):
        found["quantity"] = None  # featured before parse_recipe knew enchants carry no count
    return found


def order_key(row: dict) -> tuple:
    """A moderator's order first; the rest hardest first, then by name."""
    if row.get("position") is not None:
        return (0, row["position"], 0.0, row["name"])
    return (1, 0, -(row.get("difficulty") if row.get("difficulty") is not None else -1.0), row["name"])


def featured() -> List[dict]:
    """The featured recipes, by profession name, each profession in its order (order_key)."""
    rows = [_featured_row(r) for r in db.query("SELECT * FROM crafting_featured WHERE game = ?", (_game(),))]
    return sorted(rows, key=lambda r: ((r.get("profession_name") or "").lower(), order_key(r)))


def custom_order(rows: List[dict], profession_id: Optional[int]) -> bool:
    """Whether a moderator ordered this profession's featured recipes."""
    return any(r.get("position") is not None for r in rows if r.get("profession_id") == profession_id)


def set_order(profession_id: int, recipe_ids: Optional[List[int]]):
    """A moderator's order of a profession's featured recipes (every one of
    them, top first), or None to go back to hardest first."""
    rows = [r for r in featured() if r["profession_id"] == profession_id]
    if recipe_ids is None:
        db.execute("UPDATE crafting_featured SET position = NULL WHERE game = ? AND profession_id = ?",
                   (_game(), profession_id))
        return
    if sorted(recipe_ids) != sorted(r["recipe_id"] for r in rows):
        raise CraftingError("The order has to list every featured recipe of that profession once. Reload the page "
                            "and try again.")
    with db.transaction() as conn:
        for position, recipe_id in enumerate(recipe_ids):
            conn.execute("UPDATE crafting_featured SET position = ? WHERE game = ? AND recipe_id = ?",
                         (position, _game(), recipe_id))


async def fill_difficulty(bot):
    """Recipes featured before their difficulty was kept get it from the
    catalog (once). Failures are left for the next time."""
    missing = [r for r in await asyncio.to_thread(featured) if r.get("difficulty") is None and r["profession_id"]]
    for profession_id in {r["profession_id"] for r in missing}:
        try:
            catalog = await recipe_catalog(bot, profession_id)
        except CraftingError:
            continue
        for r in missing:
            place = place_in_catalog(catalog, r["recipe_id"]) if r["profession_id"] == profession_id else None
            if place:
                await asyncio.to_thread(
                    db.execute, "UPDATE crafting_featured SET category = ?, difficulty = ? WHERE game = ? "
                                "AND recipe_id = ?", (place["category"], place["difficulty"], _game(), r["recipe_id"]))


async def add_featured(bot, recipe_id: int, profession: dict, *, by: str, place: Optional[dict] = None) -> dict:
    """Features a recipe; `profession` is {id, name} from the catalog it was
    picked from (Blizzard's recipe document doesn't name its profession),
    `place` its {category, difficulty} there. In a profession a moderator
    ordered, it goes last."""
    if any(r["recipe_id"] == int(recipe_id) for r in await asyncio.to_thread(featured)):
        raise CraftingError("That recipe is already featured.")
    details = await recipe_details(bot, recipe_id)
    await asyncio.to_thread(
        db.execute,
        "INSERT INTO crafting_featured (game, recipe_id, name, profession_id, profession_name, item_id, item_name, "
        "quantity, reagents, icon_url, added_by, added_at, category, difficulty, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, (SELECT MAX(position) + 1 FROM crafting_featured "
        "WHERE game = ? AND profession_id = ?))",
        (_game(), int(recipe_id), details["name"], profession.get("id"), profession.get("name"), details["item_id"],
         details["item_name"], details["quantity"], json.dumps(details["reagents"]), details["icon_url"], by,
         roster.timestamp(), (place or {}).get("category"), (place or {}).get("difficulty"), _game(),
         profession.get("id")))
    return next(r for r in await asyncio.to_thread(featured) if r["recipe_id"] == int(recipe_id))


def remove_featured(recipe_id: int) -> dict:
    found = next((r for r in featured() if r["recipe_id"] == int(recipe_id)), None)
    if found is None:
        raise CraftingError("That recipe isn't featured (any more).")
    db.execute("DELETE FROM crafting_featured WHERE game = ? AND recipe_id = ?", (_game(), int(recipe_id)))
    return found


# --- who can make what ------------------------------------------------------------------------------

def crafters() -> List[dict]:
    """The characters crafting looks at: the guild's members, or every
    linked character when no guild is set. Highest level first."""
    guild = wow_api.slug(config.GUILD_ROSTER_GUILD or "")
    return [c for c in roster.characters() if (c["guild_slug"] == guild if guild else c["discord_id"])]


def who_knows(characters: List[dict]) -> Dict[int, List[dict]]:
    """{recipe id: [characters that know it]}."""
    found: Dict[int, List[dict]] = {}
    for c in characters:
        for recipe_id in roster.known_recipes(c):
            found.setdefault(recipe_id, []).append(c)
    return found


def profession_of(c: dict, profession_id: Optional[int]) -> dict:
    """The character's {skill, max} in a profession ({} when they don't have it)."""
    return next((p for p in roster.professions(c) if p.get("id") == profession_id), {})


def skill_in(c: dict, profession_id: Optional[int]) -> int:
    return profession_of(c, profession_id).get("skill") or 0


def by_skill(characters: List[dict], profession_id: Optional[int]) -> List[dict]:
    """Highest skill in the profession first, then by name."""
    return sorted(characters, key=lambda c: (-skill_in(c, profession_id), roster.display_name(c)))


def profession_overview(characters: List[dict]) -> Dict[int, List[dict]]:
    """{profession id: [{character, skill, max}]} of primary and secondary
    professions, highest skill first."""
    found: Dict[int, List[dict]] = {}
    for c in characters:
        for p in roster.professions(c):
            if p.get("id"):
                found.setdefault(p["id"], []).append({"character": c, "skill": p.get("skill"), "max": p.get("max")})
    for rows in found.values():
        rows.sort(key=lambda r: (-(r["skill"] or 0), roster.display_name(r["character"])))
    return found
