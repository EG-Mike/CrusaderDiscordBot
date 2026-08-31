"""
Static TBC World of Warcraft reference data: class colors, which roles/specs
each class can play, and the WCL percentile / item quality color scales.
None of this is specific to any one guild or server - it's true for every
deployment of this bot and should never need touching when setting one up.
Anything that DOES vary per-deployment (Discord IDs, tier progress, feature
flags, tuning knobs, logging) lives in deployment.py instead.
"""

# --- TBC class colors (Warrior..Druid only - no DK/Monk/DH in TBC) ---
CLASS_COLORS = {
    "Warrior": 0xC79C6E,
    "Paladin": 0xF58CBA,
    "Hunter": 0xABD473,
    "Rogue": 0xFFF569,
    "Priest": 0xFFFFFF,
    "Shaman": 0x0070DE,
    "Mage": 0x69CCF0,
    "Warlock": 0x9482C9,
    "Druid": 0xFF7D0A,
}

ROLES = ["Tank", "Healer", "DPS"]

# Which roles each class can actually perform in TBC - used to filter the
# role dropdown in the application dialog (e.g. a Priest never sees "Tank").
CLASS_ROLES = {
    "Warrior": ["Tank", "DPS"],
    "Paladin": ["Tank", "Healer", "DPS"],
    "Hunter": ["DPS"],
    "Rogue": ["DPS"],
    "Priest": ["Healer", "DPS"],
    "Shaman": ["Healer", "DPS"],
    "Mage": ["DPS"],
    "Warlock": ["DPS"],
    "Druid": ["Tank", "Healer", "DPS"],
}

# TBC talent trees per class - static game data, safe to hardcode.
CLASS_SPECS = {
    "Warrior": ["Arms", "Fury", "Protection"],
    "Paladin": ["Holy", "Protection", "Retribution"],
    "Hunter": ["Beast Mastery", "Marksmanship", "Survival"],
    "Rogue": ["Assassination", "Combat", "Subtlety"],
    "Priest": ["Discipline", "Holy", "Shadow"],
    "Shaman": ["Elemental", "Enhancement", "Restoration"],
    "Mage": ["Arcane", "Fire", "Frost"],
    "Warlock": ["Affliction", "Demonology", "Destruction"],
    "Druid": ["Balance", "Feral Combat", "Restoration"],
}

# Suggested default role per spec (pre-selects a sensible option in the
# applicant dialog - the applicant can still override it, since specs like
# Feral Combat can be either Tank or DPS depending on how they play it).
SPEC_DEFAULT_ROLE = {
    "Protection": "Tank",
    "Holy": "Healer",
    "Discipline": "Healer",
    "Restoration": "Healer",
}

# --- WCL percentile color scale (verify against current WCL palette if it
# ever changes - this was accurate as of the last time this was checked) ---
# List of (min_percentile, max_percentile, hex_color)
PERCENTILE_COLORS = [
    (0, 24, 0x666666),   # grey
    (25, 49, 0x1EFF00),  # green
    (50, 74, 0x0070FF),  # blue
    (75, 94, 0xA335EE),  # purple
    (95, 98, 0xFF8000),  # orange
    (99, 99, 0xE268A8),  # pink
    (100, 100, 0xE5CC80),  # gold
]


def color_for_percentile(pct) -> int:
    """Returns the hex int color for a given percentile (0-100), grey if unknown."""
    if pct is None:
        return 0x666666
    pct = round(pct)
    for lo, hi, color in PERCENTILE_COLORS:
        if lo <= pct <= hi:
            return color
    return 0x666666


# WoW item quality (as returned by Wowhead) -> embed/accent color.
ITEM_QUALITY_COLORS = {
    0: 0x9D9D9D,  # poor (grey)
    1: 0xFFFFFF,  # common (white)
    2: 0x1EFF00,  # uncommon (green)
    3: 0x0070DD,  # rare (blue)
    4: 0xA335EE,  # epic (purple)
    5: 0xFF8000,  # legendary (orange)
}


def color_for_item_quality(quality) -> int:
    """Returns the hex int color for a WoW item quality (0-5), white if unknown."""
    if quality is None:
        return ITEM_QUALITY_COLORS[1]
    return ITEM_QUALITY_COLORS.get(quality, ITEM_QUALITY_COLORS[1])
