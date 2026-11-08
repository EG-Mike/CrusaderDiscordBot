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


def parse_recipe(data: dict) -> dict:
    """What a recipe makes and needs, from Blizzard's recipe document."""
    item = data.get("crafted_item") or data.get("alliance_crafted_item") or data.get("horde_crafted_item") or {}
    return {
        "recipe_id": data.get("id"),
        "name": _name(data.get("name")) or "?",
        "item_id": item.get("id"),
        "item_name": _name(item.get("name")) or None,
        "quantity": (data.get("crafted_quantity") or {}).get("value"),
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
    return found


def featured() -> List[dict]:
    """The featured recipes, by profession and name."""
    rows = db.query("SELECT * FROM crafting_featured WHERE game = ? ORDER BY profession_name, name", (_game(),))
    return [_featured_row(r) for r in rows]


async def add_featured(bot, recipe_id: int, profession: dict, *, by: str) -> dict:
    """Features a recipe; `profession` is {id, name} from the catalog it was
    picked from (Blizzard's recipe document doesn't name its profession)."""
    if any(r["recipe_id"] == int(recipe_id) for r in await asyncio.to_thread(featured)):
        raise CraftingError("That recipe is already featured.")
    details = await recipe_details(bot, recipe_id)
    await asyncio.to_thread(
        db.execute,
        "INSERT INTO crafting_featured (game, recipe_id, name, profession_id, profession_name, item_id, item_name, "
        "quantity, reagents, icon_url, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_game(), int(recipe_id), details["name"], profession.get("id"), profession.get("name"), details["item_id"],
         details["item_name"], details["quantity"], json.dumps(details["reagents"]), details["icon_url"], by,
         roster.timestamp()))
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
