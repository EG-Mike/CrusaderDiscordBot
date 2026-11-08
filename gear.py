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
  - a ruleset that isn't one of the guild's (CHARACTER_RULESETS; empty =
    any), since Forever has rulesets instead of realms.
Slot lists are settings, since which slots take enchants depends on the
game version.

Character statistics (the character sheet: health, primary stats, hit,
crit, spell power, armor, defense, ...) come from the statistics document.
The review card shows the ones that matter for the applicant's role
(stat_highlights); the dashboard shows them all, grouped (STAT_GROUPS).
Blizzard leaves out fields a game version doesn't have, and so do we.

The summary is plain data (stored with an application, so a re-render
never needs Blizzard); render_lines() turns it into Discord text, with
item icons from the emoji library when available.
"""

import asyncio
import logging
from typing import Dict, List, Optional

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


# Statistics: (key in our summary, label, Blizzard field, how to read it, unit). "effective" reads
# {"base", "effective"}, "value" reads {"value", ...} (a percentage), "plain" a bare number.
STAT_DEFS = [
    ("health", "Health", "health", "plain", ""), ("power", "Mana / power", "power", "plain", ""),
    ("strength", "Strength", "strength", "effective", ""), ("agility", "Agility", "agility", "effective", ""),
    ("stamina", "Stamina", "stamina", "effective", ""), ("intellect", "Intellect", "intellect", "effective", ""),
    ("spirit", "Spirit", "spirit", "effective", ""),
    ("attack_power", "Attack power", "attack_power", "plain", ""),
    ("melee_hit", "Melee hit", "melee_hit", "value", "%"), ("melee_crit", "Melee crit", "melee_crit", "value", "%"),
    ("melee_haste", "Melee haste", "melee_haste", "value", "%"), ("expertise", "Expertise", "expertise", "plain", ""),
    ("armor_penetration", "Armor penetration", "armor_penetration", "plain", ""),
    ("main_hand_dps", "Main hand DPS", "main_hand_dps", "plain", ""),
    ("ranged_attack_power", "Ranged attack power", "ranged_attack_power", "plain", ""),
    ("ranged_hit", "Ranged hit", "ranged_hit", "value", "%"), ("ranged_crit", "Ranged crit", "ranged_crit", "value", "%"),
    ("ranged_haste", "Ranged haste", "ranged_haste", "value", "%"),
    ("spell_power", "Spell power", "spell_power", "plain", ""), ("spell_healing", "Bonus healing", "spell_healing", "plain", ""),
    ("spell_hit", "Spell hit", "spell_hit", "value", "%"), ("spell_crit", "Spell crit", "spell_crit", "value", "%"),
    ("spell_haste", "Spell haste", "spell_haste", "value", "%"),
    ("spell_penetration", "Spell penetration", "spell_penetration", "plain", ""),
    ("mana_regen", "Mana regen (not casting)", "mana_regen", "plain", ""),
    ("mana_regen_combat", "Mana regen (casting)", "mana_regen_combat", "plain", ""),
    ("armor", "Armor", "armor", "effective", ""), ("defense", "Defense", "defense", "effective", ""),
    ("dodge", "Dodge", "dodge", "value", "%"), ("parry", "Parry", "parry", "value", "%"),
    ("block", "Block", "block", "value", "%"),
    ("fire_resistance", "Fire resistance", "fire_resistance", "effective", ""),
    ("frost_resistance", "Frost resistance", "frost_resistance", "effective", ""),
    ("nature_resistance", "Nature resistance", "nature_resistance", "effective", ""),
    ("shadow_resistance", "Shadow resistance", "shadow_resistance", "effective", ""),
    ("arcane_resistance", "Arcane resistance", "arcane_resistance", "effective", ""),
]
STAT_LABELS = {key: (label, unit) for key, label, _field, _kind, unit in STAT_DEFS}
STAT_GROUPS = [
    ("General", ["health", "power", "strength", "agility", "stamina", "intellect", "spirit"]),
    ("Melee", ["attack_power", "melee_hit", "melee_crit", "melee_haste", "expertise", "armor_penetration",
               "main_hand_dps"]),
    ("Ranged", ["ranged_attack_power", "ranged_hit", "ranged_crit", "ranged_haste"]),
    ("Spell", ["spell_power", "spell_healing", "spell_hit", "spell_crit", "spell_haste", "spell_penetration",
               "mana_regen", "mana_regen_combat"]),
    ("Defense", ["armor", "defense", "dodge", "parry", "block"]),
    ("Resistances", ["fire_resistance", "frost_resistance", "nature_resistance", "shadow_resistance",
                     "arcane_resistance"]),
]
# What the review card shows per role. DPS is split by what the character's sheet says it is:
# casters have more spell power than attack power, hunters more ranged than melee attack power.
ROLE_STATS = {
    "Tank": ["health", "armor", "defense", "dodge", "parry", "block", "melee_hit"],
    "Healer": ["power", "intellect", "spirit", "spell_healing", "spell_crit", "mana_regen_combat"],
    "caster": ["power", "intellect", "spell_power", "spell_hit", "spell_crit", "spell_haste"],
    "melee": ["health", "attack_power", "strength", "agility", "melee_hit", "melee_crit", "melee_haste"],
    "ranged": ["ranged_attack_power", "agility", "ranged_hit", "ranged_crit", "ranged_haste", "intellect"],
}


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


def parse_professions(data: Optional[dict]) -> List[dict]:
    """[{name, skill, max, primary}] from the professions document."""
    found = []
    for kind in ("primaries", "secondaries"):
        for p in (data or {}).get(kind) or []:
            tiers = p.get("tiers") or []
            tier = max(tiers, key=lambda t: t.get("max_skill_points") or 0) if tiers else {}
            found.append({"name": _name((p.get("profession") or {}).get("name")) or "?",
                          "skill": tier.get("skill_points", p.get("skill_points")),
                          "max": tier.get("max_skill_points", p.get("max_skill_points")),
                          "primary": kind == "primaries"})
    return found


def parse_stats(data: Optional[dict]) -> Dict[str, float]:
    """{our stat key: number} from the statistics document; fields it
    doesn't have are left out."""
    stats: Dict[str, float] = {}
    for key, _label, field_name, kind, _unit in STAT_DEFS:
        value = (data or {}).get(field_name)
        if kind == "effective" and isinstance(value, dict):
            value = value.get("effective", value.get("base"))
        elif kind == "value" and isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stats[key] = round(float(value), 2)
    return stats


def format_stat(key: str, value) -> str:
    unit = STAT_LABELS.get(key, ("", ""))[1]
    if unit == "%":
        return f"{value:.2f}%"
    return f"{value:,.0f}" if float(value).is_integer() or abs(value) >= 100 else f"{value:.1f}"


def stat_profile(stats: Dict[str, float], role: Optional[str]) -> str:
    """Which ROLE_STATS list fits: the role, or for DPS caster/melee/ranged."""
    if role in ("Tank", "Healer"):
        return role
    if (stats.get("spell_power") or 0) > max(stats.get("attack_power") or 0, stats.get("ranged_attack_power") or 0):
        return "caster"
    if (stats.get("ranged_attack_power") or 0) > (stats.get("attack_power") or 0):
        return "ranged"
    return "melee"


def stat_highlights(stats: Dict[str, float], role: Optional[str]) -> List[dict]:
    """[{key, label, value, text}] of the stats that matter for the role."""
    return [{"key": k, "label": STAT_LABELS[k][0], "value": stats[k], "text": format_stat(k, stats[k])}
            for k in ROLE_STATS[stat_profile(stats, role)] if k in stats]


def stat_groups(stats: Dict[str, float]) -> List[dict]:
    """Every stat the character has, grouped for the dashboard."""
    groups = []
    for title, keys in STAT_GROUPS:
        rows = [{"key": k, "label": STAT_LABELS[k][0], "value": stats[k], "text": format_stat(k, stats[k])}
                for k in keys if k in stats]
        if rows:
            groups.append({"title": title, "stats": rows})
    return groups


def off_ruleset(summary: dict) -> bool:
    """True when the character plays on a ruleset that isn't one of the guild's."""
    allowed = [int(r) for r in config.CHARACTER_RULESETS or []]
    return bool(allowed) and summary.get("ruleset_id") is not None and summary["ruleset_id"] not in allowed


def summarise(profile: dict, equipment: dict, specializations: Optional[dict], *,
              class_name: str = "", professions: Optional[dict] = None, statistics: Optional[dict] = None) -> dict:
    """Plain-data gear summary from Blizzard's character documents."""
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
        "ruleset_id": (profile.get("ruleset") or {}).get("id"),
        "ruleset": _name((profile.get("ruleset") or {}).get("name")) or None,
        "professions": parse_professions(professions),
        "stats": parse_stats(statistics),
        "average_item_level": profile.get("average_item_level"),
        "equipped_item_level": profile.get("equipped_item_level"),
        "last_login": profile.get("last_login_timestamp"),
        "items": items,
        "talents": talents,
        "suggested_spec": talents[0]["spec"] if talents and talents[0]["points"] else None,
        "suggested_spec_id": talents[0]["spec_id"] if talents and talents[0]["points"] else None,
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
    """Reads the character's profile, gear, talents, professions and
    statistics from Blizzard and
    summarises them. GearError when there's nothing to read."""
    client = wow_api._client(bot)
    namespace = wow_api.namespace("profile")
    base = f"/profile/wow/character/{character_slug}"
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
        try:
            answers = await asyncio.gather(*(
                client.request(base + sub, namespace, session=session, locale=config.BLIZZARD_LOCALE)
                for sub in ("", "/equipment", "/specializations", "/professions", "/statistics")))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GearError(f"Couldn't reach Blizzard ({type(exc).__name__}). Try again in a minute.") from None
    ((p_status, p_headers, profile), (e_status, _, equipment), (_s, _, specializations), (_p, _, professions),
     (_st, _, statistics)) = answers
    if p_status == 404:
        raise GearError("Blizzard has no profile for this character. Characters can be looked up from level "
                        "10, after logging out once.")
    if p_status != 200 or e_status != 200:
        raise GearError(f"Blizzard answered {p_status if p_status != 200 else e_status}. Try again in a minute.")
    class_id = (profile.get("character_class") or {}).get("id")
    summary = summarise(profile, equipment, specializations, professions=professions, statistics=statistics,
                        class_name=(class_names or {}).get(class_id, ""))
    summary["profile_updated"] = p_headers.get("Last-Modified") or p_headers.get("last-modified")
    return summary


# --- rendering ---------------------------------------------------------------------------------------

_ICON_URLS: Dict[int, Optional[str]] = {}  # item id -> icon image (per process; icons don't change)


async def item_icon_urls(bot, summary: dict) -> Dict[int, Optional[str]]:
    """{item id: icon image URL} from Blizzard's item media, for the
    dashboard. Cached for the life of the process. Never raises."""
    client = wow_api._client(bot)
    missing = [i["item_id"] for i in summary.get("items", [])
               if i.get("item_id") and i["item_id"] not in _ICON_URLS]

    async def one(item_id, session):
        try:
            status, _headers, body = await client.request(f"/data/wow/media/item/{item_id}",
                                                          wow_api.namespace("static"), session=session)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return  # not cached: tried again next time
        assets = (body or {}).get("assets") or [] if status == 200 else []
        url = next((a.get("value") for a in assets if a.get("key") == "icon"), None)
        for candidate in wow_api.icon_urls(url):  # the first address Blizzard's image server serves
            try:
                async with session.head(candidate) as resp:
                    if resp.status == 200:
                        _ICON_URLS[item_id] = candidate
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return  # not cached: tried again next time
        _ICON_URLS[item_id] = None

    if missing:
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=15)) as session:
            await asyncio.gather(*(one(i, session) for i in missing))
    return {i["item_id"]: _ICON_URLS.get(i["item_id"]) for i in summary.get("items", []) if i.get("item_id")}


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


def render_lines(summary: dict, icons: Optional[Dict[int, str]] = None, role: Optional[str] = None) -> Dict[str, str]:
    """Discord text for a summary: {"overview", "gear", "flags"}. Compact
    on purpose: a review card has 4000 characters for everything. `role`
    picks which statistics are shown (stat_highlights)."""
    icons = icons or {}
    talents = " / ".join(f"{t['spec']} {t['points']}" for t in summary.get("talents") or []) or "none spent"
    overview = [
        f"**Item level:** {summary.get('equipped_item_level') or '?'}"
        + (f" (bags included: {summary['average_item_level']})"
           if summary.get("average_item_level") and summary.get("average_item_level") != summary.get(
               "equipped_item_level") else ""),
        f"**Talents:** {talents}",
    ]
    highlights = stat_highlights(summary.get("stats") or {}, role)
    if highlights:
        overview.append("**Stats:** " + " · ".join(f"{h['label']} {h['text']}" for h in highlights))
    trades = [f"{p['name']} {p['skill']}/{p['max']}" for p in summary.get("professions") or [] if p["primary"]]
    if trades:
        overview.append("**Professions:** " + " · ".join(trades))
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
    if off_ruleset(summary):
        flags.append(f"On the {summary.get('ruleset') or 'another'} ruleset, not one of the guild's")
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
