"""
Gear check: a character's equipped gear, talents and profile from Blizzard's
API (wow_api.py), summarised for an application's review card or a
moderator's /gearcheck character.

Blizzard updates this data when the player logs out, so a summary shows
the gear as of the character's last logout (summary["profile_updated"]).

What's flagged:
  - empty slots (GEARCHECK_EXPECTED_SLOTS: slots a geared character fills),
  - missing enchants (GEARCHECK_ENCHANT_SLOTS),
  - empty gem sockets,
  - requirements: GEARCHECK_MIN_LEVEL and GEARCHECK_MIN_ITEM_LEVEL (0 = none).
Slot lists are settings, since which slots take enchants depends on the
game version.

The summary is plain data (stored with an application, so a re-render
never needs Blizzard); render_lines() turns it into Discord text, with
item icons from the emoji library when available.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional

import aiohttp

import config
import wow_api

log = logging.getLogger("wow-apply-bot.gear")

QUALITY_MARKS = {"POOR": "⬛", "COMMON": "⬜", "UNCOMMON": "🟩", "RARE": "🟦", "EPIC": "🟪", "LEGENDARY": "🟧",
                 "ARTIFACT": "🟨", "HEIRLOOM": "🟨"}
SLOT_ORDER = ["HEAD", "NECK", "SHOULDER", "BACK", "CHEST", "SHIRT", "TABARD", "WRIST", "HANDS", "WAIST", "LEGS",
              "FEET", "FINGER_1", "FINGER_2", "TRINKET_1", "TRINKET_2", "MAIN_HAND", "OFF_HAND", "RANGED"]
SLOT_NAMES = {"HEAD": "Head", "NECK": "Neck", "SHOULDER": "Shoulders", "BACK": "Back", "CHEST": "Chest",
              "SHIRT": "Shirt", "TABARD": "Tabard", "WRIST": "Wrists", "HANDS": "Hands", "WAIST": "Waist",
              "LEGS": "Legs", "FEET": "Feet", "FINGER_1": "Ring", "FINGER_2": "Ring", "TRINKET_1": "Trinket",
              "TRINKET_2": "Trinket", "MAIN_HAND": "Main hand", "OFF_HAND": "Off hand", "RANGED": "Ranged"}
EMPTY_SLOT_NAMES = {"FINGER_1": "Ring 1", "FINGER_2": "Ring 2", "TRINKET_1": "Trinket 1", "TRINKET_2": "Trinket 2"}
COSMETIC_SLOTS = {"SHIRT", "TABARD"}  # not counted for item level or flags


class GearError(Exception):
    """The gear can't be read; the message says why."""


def _name(value) -> str:
    return wow_api._name(value)


def parse_item(it: dict) -> dict:
    """One equipped item (Blizzard's equipment entry) as plain data."""
    slot = ((it.get("slot") or {}).get("type") or "").upper()
    sockets = [{"type": _name((s.get("socket_type") or {}).get("name")) or None,
                "gem": _name((s.get("item") or {}).get("name")) or None,
                "text": s.get("display_string")} for s in it.get("sockets") or []]
    return {
        "slot": slot,
        "slot_name": SLOT_NAMES.get(slot) or _name((it.get("slot") or {}).get("name")) or slot.title(),
        "item_id": (it.get("item") or {}).get("id"),
        "name": _name(it.get("name")) or "?",
        "quality": ((it.get("quality") or {}).get("type") or "").upper() or None,
        "item_level": (it.get("level") or {}).get("value"),
        "stats": [(s.get("display") or {}).get("display_string") for s in it.get("stats") or []
                  if (s.get("display") or {}).get("display_string")],
        "enchants": [e.get("display_string") for e in it.get("enchantments") or [] if e.get("display_string")],
        "sockets": sockets,
        "set": _name(((it.get("set") or {}).get("item_set") or {}).get("name")) or None,
    }


def parse_talents(data: dict) -> List[dict]:
    """[{spec, spec_id, points}] of the active talent group, most points first."""
    groups = (data or {}).get("specialization_groups") or []
    active = next((g for g in groups if g.get("is_active")), groups[0] if groups else None)
    specs = [{"spec": _name((s.get("specialization") or {}).get("name")) or s.get("specialization_name") or "?",
              "spec_id": (s.get("specialization") or {}).get("id"),
              "points": s.get("spent_points") or 0} for s in (active or {}).get("specializations") or []]
    return sorted(specs, key=lambda s: -s["points"])


def summarise(profile: dict, equipment: dict, specializations: Optional[dict], *,
              class_name: str = "") -> dict:
    """Plain-data gear summary from the three Blizzard documents."""
    items = sorted((parse_item(it) for it in (equipment or {}).get("equipped_items") or []),
                   key=lambda i: SLOT_ORDER.index(i["slot"]) if i["slot"] in SLOT_ORDER else len(SLOT_ORDER))
    worn = {i["slot"] for i in items}
    enchant_slots = [s.upper() for s in config.GEARCHECK_ENCHANT_SLOTS or []]
    expected = [s.upper() for s in config.GEARCHECK_EXPECTED_SLOTS or []]
    talents = parse_talents(specializations)
    guild = profile.get("guild") or {}
    return {
        "character": f"{profile.get('name', '')} {profile.get('surname') or ''}".strip(),
        "slug": wow_api.slug(f"{profile.get('name', '')}-{profile.get('surname') or ''}"),
        "level": profile.get("level"),
        "class_id": (profile.get("character_class") or {}).get("id"),
        "class_name": class_name or _name((profile.get("character_class") or {}).get("name")),
        "race": _name((profile.get("race") or {}).get("name")) or None,
        "faction": _name((profile.get("faction") or {}).get("name")) or None,
        "guild": _name(guild.get("name")) or None,
        "average_item_level": profile.get("average_item_level"),
        "equipped_item_level": profile.get("equipped_item_level"),
        "last_login": profile.get("last_login_timestamp"),
        "items": items,
        "talents": talents,
        "suggested_spec": talents[0]["spec"] if talents and talents[0]["points"] else None,
        "empty_slots": [EMPTY_SLOT_NAMES.get(s) or SLOT_NAMES.get(s, s.title()) for s in expected if s not in worn],
        "missing_enchants": [i["slot_name"] for i in items if i["slot"] in enchant_slots and not i["enchants"]],
        "empty_sockets": sum(1 for i in items for s in i["sockets"] if not s["gem"]),
    }


def requirement_problems(summary: dict) -> List[str]:
    """What keeps the character from the guild's minimums (empty: fine or none set)."""
    problems = []
    min_level = int(config.GEARCHECK_MIN_LEVEL or 0)
    if min_level and (summary.get("level") or 0) < min_level:
        problems.append(f"level {summary.get('level') or '?'} (needs {min_level})")
    min_ilvl = int(config.GEARCHECK_MIN_ITEM_LEVEL or 0)
    if min_ilvl and (summary.get("equipped_item_level") or 0) < min_ilvl:
        problems.append(f"item level {summary.get('equipped_item_level') or '?'} (needs {min_ilvl})")
    return problems


async def fetch(bot, character_slug: str, class_names: Optional[Dict[int, str]] = None) -> dict:
    """Reads the character's profile, gear and talents from Blizzard and
    summarises them. GearError when there's nothing to read."""
    client = wow_api._client(bot)
    namespace = wow_api.namespace("profile")
    base = f"/profile/wow/character/{character_slug}"
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
        try:
            answers = await asyncio.gather(*(
                client.request(base + sub, namespace, session=session, locale=config.BLIZZARD_LOCALE)
                for sub in ("", "/equipment", "/specializations")))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GearError(f"Couldn't reach Blizzard ({type(exc).__name__}). Try again in a minute.") from None
    (p_status, p_headers, profile), (e_status, _, equipment), (_s_status, _, specializations) = answers
    if p_status == 404:
        raise GearError("Blizzard has no profile for this character. Characters can be looked up from level "
                        "10, after logging out once.")
    if p_status != 200 or e_status != 200:
        raise GearError(f"Blizzard answered {p_status if p_status != 200 else e_status}. Try again in a minute.")
    class_id = (profile.get("character_class") or {}).get("id")
    summary = summarise(profile, equipment, specializations,
                        class_name=(class_names or {}).get(class_id, ""))
    summary["profile_updated"] = p_headers.get("Last-Modified") or p_headers.get("last-modified")
    return summary


# --- rendering ---------------------------------------------------------------------------------------

IconFor = Callable[[dict], Awaitable[str]]


async def item_icons(bot, summary: dict) -> Dict[int, str]:
    """{item id: inline emoji} from the emoji library ('' when one can't be
    made). Never raises."""
    import emoji_library

    async def one(item):
        try:
            return item["item_id"], await emoji_library.ensure_icon(bot, "item", item["item_id"],
                                                                    by="automatic (gear check)")
        except Exception:
            log.debug("No icon for item %s", item["item_id"], exc_info=True)
            return item["item_id"], ""

    pairs = await asyncio.gather(*(one(i) for i in summary.get("items", []) if i.get("item_id")))
    return dict(pairs)


def render_lines(summary: dict, icons: Optional[Dict[int, str]] = None) -> Dict[str, str]:
    """Discord text for a summary: {"overview", "gear", "flags"}. Compact
    on purpose: a review card has 4000 characters for everything."""
    icons = icons or {}
    talents = " / ".join(f"{t['spec']} {t['points']}" for t in summary.get("talents") or []) or "none spent"
    overview = [
        f"**Item level:** {summary.get('equipped_item_level') or '?'}"
        + (f" (bags included: {summary['average_item_level']})"
           if summary.get("average_item_level") and summary.get("average_item_level") != summary.get(
               "equipped_item_level") else ""),
        f"**Talents:** {talents}",
    ]
    problems = requirement_problems(summary)
    if config.GEARCHECK_MIN_LEVEL or config.GEARCHECK_MIN_ITEM_LEVEL:
        overview.append("**Requirements:** " + ("✅ met" if not problems else "⚠️ below on " + ", ".join(problems)))

    gear = []
    for item in summary.get("items") or []:
        if item["slot"] in COSMETIC_SLOTS:
            continue
        mark = icons.get(item.get("item_id")) or QUALITY_MARKS.get(item.get("quality") or "", "▫️")
        extras = item["enchants"] + [s["gem"] or f"empty {s['type'] or 'socket'}" for s in item["sockets"]]
        line = f"{mark} **{item['name']}** `{item.get('item_level') or '?'}` · {item['slot_name']}"
        if item.get("set"):
            line += f" · {item['set']}"
        if extras:
            line += "\n-# " + " · ".join(extras)
        gear.append(line)

    flags = []
    if summary.get("empty_slots"):
        flags.append("Empty: " + ", ".join(summary["empty_slots"]))
    if summary.get("missing_enchants"):
        flags.append("No enchant: " + ", ".join(summary["missing_enchants"]))
    if summary.get("empty_sockets"):
        flags.append(f"{summary['empty_sockets']} empty gem socket{'s' if summary['empty_sockets'] != 1 else ''}")
    return {
        "overview": "\n".join(overview),
        "gear": "\n".join(gear) or "No gear equipped.",
        "flags": ("⚠️ " + " · ".join(flags)) if flags else "",
    }
