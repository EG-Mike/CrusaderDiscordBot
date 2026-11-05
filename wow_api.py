"""
Blizzard's WoW API for the configured game version (WoW Forever by default):
reference data for pickers, characters from a player's Battle.net account,
and character lookups by name.

Everything game-version-specific is a setting (config/deployment.py, "Game &
Blizzard API"): the namespaces (BLIZZARD_*_NAMESPACE, region added from
SERVER_REGION), the locale and the character lookup paths.

What WoW Forever's API offered at launch (see docs/wow-forever-api.md):
  - static-forever: classes, specs, races, professions, items, ... (200)
  - dynamic-forever: regions and rulesets (200). Forever has no realms:
    players pick a ruleset (Normal, PvP, Roleplay, Roleplay PvP), and the
    realm index that existed before launch is gone (404).
  - profile-forever: a player's own account profile with a user token
    (/profile/user/wow, scope wow.profile) lists their characters with
    name, surname, class, race, faction and level, at every level.
    Public character profiles (/profile/wow/character/{first}-{surname},
    from level 10, updated at logout) and guilds (/data/wow/guild/{slug},
    whose roster lists every member's level) answer with the app's token.
  A namespace that doesn't exist answers 403; an existing one with nothing
  at that path answers 404.
"""

import asyncio
import logging
import re
import string
import time
from typing import Dict, List, Optional

import aiohttp

import config

log = logging.getLogger("wow-apply-bot.wow_api")

REFERENCE_TTL_SECONDS = 6 * 3600
EXPLORE_PREFIXES = ("/data/wow/", "/profile/wow/")
REGIONS = ("us", "eu", "kr", "tw")
ROLES = {"TANK": "tank", "HEALER": "healer", "DAMAGE": "dps"}  # Blizzard's spec role types
_PROTECTED_RE = re.compile(r"/protected-character/(\d+)-(\d+)")


class WowApiError(Exception):
    """Blizzard data isn't available; the message says why."""


def region() -> str:
    return (config.SERVER_REGION or "us").lower()


def namespace(kind: str, for_region: Optional[str] = None) -> str:
    """'static' | 'dynamic' | 'profile' -> e.g. 'static-forever-eu'."""
    family = {"static": config.BLIZZARD_STATIC_NAMESPACE, "dynamic": config.BLIZZARD_DYNAMIC_NAMESPACE,
              "profile": config.BLIZZARD_PROFILE_NAMESPACE}[kind]
    return f"{family}-{for_region or region()}"


def _client(bot):
    client = getattr(bot, "blizzard", None)
    if client is None:
        raise WowApiError("Blizzard API keys aren't set up (Settings, Integrations).")
    return client


def _name(value) -> str:
    if isinstance(value, dict):
        return value.get(config.BLIZZARD_LOCALE) or value.get("en_US") or next(iter(value.values()), "")
    return value or ""


# --- reference data (classes, specs, races, professions, rulesets) ---------------------------

_reference: Dict[str, object] = {"data": None, "at": 0.0, "key": None}


async def reference(bot, *, fresh: bool = False) -> dict:
    """{classes: [{id, name, specs: [{id, name, role}]}] (role: tank/healer/dps or None), races, professions,
    rulesets: [{id, name}], errors: [...]} for the configured game,
    cached for a few hours. Parts that fail are listed in `errors` instead
    of failing the whole lookup."""
    key = (namespace("static"), namespace("dynamic"), config.BLIZZARD_LOCALE)
    if (not fresh and _reference["data"] is not None and _reference["key"] == key
            and time.monotonic() - _reference["at"] < REFERENCE_TTL_SECONDS):
        return _reference["data"]
    client = _client(bot)
    errors: List[str] = []
    data = {"classes": [], "races": [], "professions": [], "rulesets": [], "errors": errors,
            "game": config.GAME_VERSION, "region": region()}

    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def get(path, kind):
            status, _headers, body = await client.request(path, namespace(kind), session=session,
                                                          locale=config.BLIZZARD_LOCALE)
            if status != 200:
                errors.append(f"{path} ({namespace(kind)}): HTTP {status}")
                return None
            return body

        classes = await get("/data/wow/playable-class/index", "static")
        if classes:
            details = await asyncio.gather(*(get(f"/data/wow/playable-class/{c['id']}", "static")
                                             for c in classes.get("classes", [])))
            spec_ids = [s["id"] for detail in details for s in (detail or {}).get("specializations", [])]
            spec_details = await asyncio.gather(*(get(f"/data/wow/playable-specialization/{i}", "static")
                                                  for i in spec_ids))
            roles = {i: ((d or {}).get("role") or {}).get("type") for i, d in zip(spec_ids, spec_details)}
            for c, detail in zip(classes.get("classes", []), details):
                specs = [{"id": s["id"], "name": _name(s.get("name")), "role": ROLES.get(roles.get(s["id"]))}
                         for s in (detail or {}).get("specializations", [])]
                data["classes"].append({"id": c["id"], "name": _name(c.get("name")), "specs": specs})
        races = await get("/data/wow/playable-race/index", "static")
        data["races"] = sorted(({"id": r["id"], "name": _name(r.get("name"))} for r in (races or {}).get("races", [])),
                               key=lambda r: r["name"])
        professions = await get("/data/wow/profession/index", "static")
        data["professions"] = sorted(
            ({"id": p["id"], "name": _name(p.get("name"))} for p in (professions or {}).get("professions", [])
             if "[DNT]" not in _name(p.get("name"))),  # Blizzard's internal test entries
            key=lambda p: p["name"])
        rulesets = await get("/data/wow/ruleset/index", "dynamic")
        ruleset_ids = [r["id"] for r in (rulesets or {}).get("rulesets", []) if "id" in r]
        details = await asyncio.gather(*(get(f"/data/wow/ruleset/{i}", "dynamic") for i in ruleset_ids))
        data["rulesets"] = sorted(({"id": i, "name": _name((d or {}).get("name")) or f"Ruleset {i}"}
                                   for i, d in zip(ruleset_ids, details)), key=lambda r: r["id"])

    if errors:
        log.warning("Some Blizzard reference data is unavailable: %s", "; ".join(errors))
    _reference.update(data=data, at=time.monotonic(), key=key)
    return data


# --- a player's own account (Battle.net login) --------------------------------------------------

def parse_account_profile(data: dict) -> List[dict]:
    """Characters from /profile/user/wow. WoW Forever characters have a
    first name and a surname and no realm object; the protected-character
    link still carries an internal realm ID ("protected-character/
    6855-716333"), kept as realm_id though Forever shows players rulesets,
    not realms."""
    characters = []
    for account in (data or {}).get("wow_accounts", []):
        for c in account.get("characters", []):
            realm_id, character_id = None, c.get("id")
            match = _PROTECTED_RE.search(((c.get("protected_character") or {}).get("href")) or "")
            if match:
                realm_id, character_id = int(match[1]), int(match[2])
            realm = c.get("realm") or {}
            characters.append({
                "blizzard_id": character_id,
                "name": c.get("name"),
                "surname": c.get("surname") or None,
                "realm_id": realm.get("id") or realm_id,
                "realm_slug": realm.get("slug"),
                "realm_name": _name(realm.get("name")) or None,
                "level": c.get("level"),
                "class_id": (c.get("playable_class") or {}).get("id"),
                "class_name": _name((c.get("playable_class") or {}).get("name")) or None,
                "race_name": _name((c.get("playable_race") or {}).get("name")) or None,
                "faction": ((c.get("faction") or {}).get("type") or "").title() or None,
                "account_id": account.get("id"),
            })
    return [c for c in characters if c["name"]]


async def account_characters(bot, user_token: str) -> List[dict]:
    """The characters on the Battle.net account a user token belongs to.
    Blizzard answers 404 for an account without characters in this game."""
    status, _headers, body = await _client(bot).request("/profile/user/wow", namespace("profile"),
                                                        token=user_token, locale=config.BLIZZARD_LOCALE)
    if status == 404:
        return []
    if status in (401, 403):
        raise WowApiError("The Battle.net login expired or wasn't allowed to read characters.")
    if status != 200:
        raise WowApiError(f"Blizzard answered {status} for the account profile.")
    return parse_account_profile(body)


# --- character lookups by name --------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")


LOOKUP_FIELDS = {"name", "surname"}


async def lookup_character(bot, name: str, surname: Optional[str] = None,
                           session: Optional[aiohttp.ClientSession] = None) -> Optional[dict]:
    """A character's public profile, trying CHARACTER_LOOKUP_PATHS in order
    ({name} and {surname} are filled in; Forever names are unique per
    region, so there's no realm). None when no path answered 200, e.g. a
    character below level 10, which has no public profile."""
    client = _client(bot)
    for template in config.CHARACTER_LOOKUP_PATHS or []:
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
        if fields - LOOKUP_FIELDS:
            log.warning("Character lookup path %r uses unknown placeholders %s - skipped", template,
                        ", ".join(sorted(fields - LOOKUP_FIELDS)))
            continue
        if "surname" in fields and not surname:
            continue
        path = template.format(name=_slug(name), surname=_slug(surname or ""))
        status, _headers, body = await client.request(path, namespace("profile"), session=session,
                                                      locale=config.BLIZZARD_LOCALE)
        if status == 200 and body:
            return body
    return None


def slug(text: str) -> str:
    """Blizzard's URL form of a name: "Eggmike Thewhite" -> "eggmike-thewhite"."""
    return _slug(text)


def _id(value) -> Optional[int]:
    return (value or {}).get("id") if isinstance(value, dict) else None


def parse_character(c: dict, *, rank: Optional[int] = None) -> dict:
    """A character from a guild roster entry or a character profile, as
    the roster stores it. Missing fields are None (a roster entry has no
    guild; a profile has no rank)."""
    guild = c.get("guild") or {}
    return {
        "character_id": c.get("id"),
        "slug": c.get("slug") or _slug(f"{c.get('name', '')}-{c.get('surname') or ''}"),
        "name": c.get("name"),
        "surname": c.get("surname") or None,
        "class_id": _id(c.get("playable_class") or c.get("character_class")),
        "race_id": _id(c.get("playable_race") or c.get("race")),
        "faction": ((c.get("faction") or {}).get("type") or "").title() or None,
        "level": c.get("level") if isinstance(c.get("level"), int) else None,
        "ruleset_id": _id(c.get("ruleset")),
        "guild_slug": guild.get("slug") or (_slug(guild["name"]) if guild.get("name") else None),
        "guild_rank": rank,
        "last_login": c.get("last_login_timestamp"),
    }


async def guild_roster(bot, guild_name: str, session: Optional[aiohttp.ClientSession] = None) -> Optional[List[dict]]:
    """Every member of a guild (parse_character, with their rank), at every
    level. None when Blizzard doesn't know the guild. The roster is a
    snapshot Blizzard refreshes every few hours."""
    status, _headers, body = await _client(bot).request(f"/data/wow/guild/{_slug(guild_name)}/roster",
                                                        namespace("profile"), session=session,
                                                        locale=config.BLIZZARD_LOCALE)
    if status == 404:
        return None
    if status != 200:
        raise WowApiError(f"Blizzard answered {status} for the guild roster.")
    return [{**parse_character(m.get("character") or {}, rank=m.get("rank")),
             "guild_slug": _slug(guild_name)} for m in (body or {}).get("members", [])]


async def character_profile(bot, character_slug: str,
                            session: Optional[aiohttp.ClientSession] = None) -> Optional[dict]:
    """One character's profile (parse_character) by its slug, or None when
    there's none: below level 10, never logged out, or unknown. Updated by
    Blizzard when the player logs out."""
    status, _headers, body = await _client(bot).request(f"/profile/wow/character/{character_slug}",
                                                        namespace("profile"), session=session,
                                                        locale=config.BLIZZARD_LOCALE)
    if status == 404:
        return None
    if status != 200:
        raise WowApiError(f"Blizzard answered {status} for character {character_slug}.")
    return parse_character(body or {})


def profile_level(profile: dict) -> Optional[int]:
    level = (profile or {}).get("level")
    return int(level) if isinstance(level, int) else None


# --- the admin API explorer -----------------------------------------------------------------------

async def explore(bot, path: str, namespace_value: str, for_region: Optional[str] = None) -> dict:
    """One raw GET for debugging (Maintenance, Blizzard API explorer). Only
    WoW data and profile paths, with the app's own token."""
    path = "/" + (path or "").strip().lstrip("/")
    if not path.startswith(EXPLORE_PREFIXES) or ".." in path:
        raise WowApiError("Only /data/wow/... and /profile/wow/... paths can be explored.")
    for_region = (for_region or region()).lower()
    if for_region not in REGIONS:
        raise WowApiError("Unknown region.")
    if not re.fullmatch(r"[a-z0-9._-]{3,80}", namespace_value or ""):
        raise WowApiError("That namespace doesn't look right (e.g. static-forever-eu).")
    path_only, _, query = path.partition("?")
    params = dict(part.split("=", 1) for part in query.split("&") if "=" in part) if query else None
    started = time.monotonic()
    status, headers, body = await _client(bot).request(path_only, namespace_value, params, region=for_region,
                                                        locale=config.BLIZZARD_LOCALE)
    keep = ("battlenet-namespace", "battlenet-schema", "battlenet-schema-revision", "last-modified", "cache-control")
    return {
        "status": status,
        "meaning": {200: "Found", 403: "Namespace doesn't exist (or isn't open)",
                    404: "Namespace exists, but nothing at this path"}.get(status, ""),
        "ms": round((time.monotonic() - started) * 1000),
        "headers": {k: v for k, v in headers.items() if k.lower() in keep},
        "body": body,
    }
