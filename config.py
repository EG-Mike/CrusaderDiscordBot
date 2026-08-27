"""
Config constants: TBC class colors, WCL percentile color scale, and current/
previous raid tier definitions.

TIER TRANSITION: when Sunwell Plateau releases, update CURRENT_TIER below to
FUTURE_TIERS["Sunwell Plateau"], and set PREVIOUS_TIER to today's
CURRENT_TIER value (BT/Hyjal). That's the one-line switch.

All zone/encounter IDs below were pulled directly from WCL's
`worldData.zones` query against the fresh.warcraftlogs.com host (via
debug_zones.py) - not guessed, so they should be accurate.
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

# Fallback guild emoji names, used only if a class/role isn't in
# CLASS_ICON_URLS / ROLE_ICON_URLS below (e.g. you already manually
# uploaded some before switching to the URL-based method, or just prefer
# managing a specific one that way).
CLASS_EMOJI_NAMES = {
    "Warrior": "Classicons_warrior",
    "Paladin": "Classicons_paladin",
    "Hunter": "Classicons_hunter",
    "Rogue": "Classicons_rogue",
    "Priest": "Classicons_priest",
    "Shaman": "Classicons_shaman",
    "Mage": "Classicons_mage",
    "Warlock": "Classicons_warlock",
    "Druid": "Classicons_druid",
}

ROLE_EMOJI_NAMES = {
    "Tank": "Roleicon_tank",
    "Healer": "Roleicon_healer",
    "DPS": "Roleicon_dps",
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

# --- Icon sourcing ---
# All class/role/spec icons use the same mechanism: bot-owned "application
# emoji". On startup, the bot downloads each URL below once and uploads it
# as an emoji owned by the BOT ITSELF (not any one server) via Discord's
# application-emoji API - so you never have to manually upload anything.
# A Wowhead-hosted icon URL works well, e.g.
# https://wow.zamimg.com/images/wow/icons/large/spell_holy_holybolt.jpg
#
# Class/role icons fall back to a same-named guild emoji (via
# CLASS_EMOJI_NAMES / ROLE_EMOJI_NAMES above) if no URL is configured here -
# useful if you already uploaded some manually and don't want to redo them.
# Spec icons have no such fallback since they were never manual to begin
# with.
CLASS_ICON_URLS = {
    "Warrior": "https://wow.zamimg.com/images/wow/icons/large/classicon_warrior.jpg",
    "Paladin": "https://wow.zamimg.com/images/wow/icons/large/classicon_paladin.jpg",
    "Hunter":  "https://wow.zamimg.com/images/wow/icons/large/classicon_hunter.jpg",
    "Rogue":   "https://wow.zamimg.com/images/wow/icons/large/classicon_rogue.jpg",
    "Priest":  "https://wow.zamimg.com/images/wow/icons/large/classicon_priest.jpg",
    "Shaman":  "https://wow.zamimg.com/images/wow/icons/large/classicon_shaman.jpg",
    "Mage":    "https://wow.zamimg.com/images/wow/icons/large/classicon_mage.jpg",
    "Warlock": "https://wow.zamimg.com/images/wow/icons/large/classicon_warlock.jpg",
    "Druid":   "https://wow.zamimg.com/images/wow/icons/large/classicon_druid.jpg",
}


ROLE_ICON_URLS = {
     "Tank": "https://wow.zamimg.com/images/wow/icons/large/ability_warrior_defensivestance.jpg",
     "Healer": "https://wow.zamimg.com/images/wow/icons/large/spell_holy_holybolt.jpg",
     "DPS": "https://wow.zamimg.com/images/wow/icons/large/ability_dualwield.jpg",
}

# Key format: "ClassName:SpecName" (must match CLASS_SPECS above exactly).
SPEC_ICON_URLS = {
    # Warrior
    "Warrior:Arms":       "https://wow.zamimg.com/images/wow/icons/large/ability_rogue_eviscerate.jpg",
    "Warrior:Fury":       "https://wow.zamimg.com/images/wow/icons/large/ability_warrior_innerrage.jpg",
    "Warrior:Protection": "https://wow.zamimg.com/images/wow/icons/large/ability_warrior_defensivestance.jpg",

    # Paladin
    "Paladin:Holy":       "https://wow.zamimg.com/images/wow/icons/large/spell_holy_holybolt.jpg",
    "Paladin:Protection": "https://wow.zamimg.com/images/wow/icons/large/spell_holy_devotionaura.jpg",
    "Paladin:Retribution":"https://wow.zamimg.com/images/wow/icons/large/spell_holy_auraoflight.jpg",

    # Hunter
    "Hunter:Beast Mastery": "https://wow.zamimg.com/images/wow/icons/large/ability_hunter_bestialdiscipline.jpg",
    "Hunter:Marksmanship":  "https://wow.zamimg.com/images/wow/icons/large/ability_hunter_focusedaim.jpg",
    "Hunter:Survival":      "https://wow.zamimg.com/images/wow/icons/large/ability_hunter_swiftstrike.jpg",

    # Rogue
    "Rogue:Assassination": "https://wow.zamimg.com/images/wow/icons/large/ability_rogue_eviscerate.jpg",
    "Rogue:Combat":        "https://wow.zamimg.com/images/wow/icons/large/ability_warrior_innerrage.jpg",
    "Rogue:Subtlety":      "https://wow.zamimg.com/images/wow/icons/large/ability_stealth.jpg",

    # Priest
    "Priest:Discipline":   "https://wow.zamimg.com/images/wow/icons/large/spell_holy_powerwordshield.jpg",
    "Priest:Holy":         "https://wow.zamimg.com/images/wow/icons/large/spell_holy_holybolt.jpg",
    "Priest:Shadow":       "https://wow.zamimg.com/images/wow/icons/large/spell_shadow_shadowwordpain.jpg",

    # Shaman
    "Shaman:Elemental":    "https://wow.zamimg.com/images/wow/icons/large/spell_nature_lightning.jpg",
    "Shaman:Enhancement":  "https://wow.zamimg.com/images/wow/icons/large/spell_shaman_improvedstormstrike.jpg",
    "Shaman:Restoration":  "https://wow.zamimg.com/images/wow/icons/large/spell_nature_magicimmunity.jpg",

    # Mage
    "Mage:Arcane":         "https://wow.zamimg.com/images/wow/icons/large/spell_holy_magicalsentry.jpg",
    "Mage:Fire":           "https://wow.zamimg.com/images/wow/icons/large/spell_fire_firebolt02.jpg",
    "Mage:Frost":          "https://wow.zamimg.com/images/wow/icons/large/spell_frost_frostbolt02.jpg",

    # Warlock
    "Warlock:Affliction":  "https://wow.zamimg.com/images/wow/icons/large/spell_shadow_deathcoil.jpg",
    "Warlock:Demonology":  "https://wow.zamimg.com/images/wow/icons/large/spell_shadow_metamorphosis.jpg",
    "Warlock:Destruction": "https://wow.zamimg.com/images/wow/icons/large/spell_shadow_rainoffire.jpg",

    # Druid
    "Druid:Balance":       "https://wow.zamimg.com/images/wow/icons/large/spell_nature_starfall.jpg",
    "Druid:Feral":         "https://wow.zamimg.com/images/wow/icons/large/ability_racial_bearform.jpg",
    "Druid:Restoration":   "https://wow.zamimg.com/images/wow/icons/large/spell_nature_healingtouch.jpg",
    #add guardian druid (feral tank)
}


# Max gear screenshots shown per application (each is a separate embed
# stacked in the same message - Discord caps a message at 10 embeds total,
# and one of those is the main info embed).
MAX_SCREENSHOTS = 4

# OXM.gg's /register command ID - used to render a clickable slash-command
# mention in the approval DM (</register:ID> - Discord formats this as a
# highlighted command reference the applicant can click to pre-fill it,
# regardless of which bot owns the command). Command IDs are tied to the
# bot's application, not any one server, so this shouldn't need to change
# when moving from a test server to the real one - but worth double
# checking if OXM ever re-registers their commands.
OXM_REGISTER_COMMAND_ID = 1502459742461624360

# Roles that, if an approved applicant already holds ANY of them, mean the
# "Fresh" role should NOT be (re-)assigned or should be reported as
# "already had" instead of "assigned" - they're already a step above plain
# Fresh status. Also used to skip the nickname change for the same people.
# Maps role ID -> display name, so the approval card can say exactly which
# of these (Fresh/Regular/Organizer) the applicant actually holds instead of
# a generic combined label. Dict membership (`in`) checks the keys, same as
# the old plain list did, so existing `id in FRESH_EXEMPT_ROLE_IDS` checks
# still work unchanged.
FRESH_EXEMPT_ROLE_IDS = {
    1337919809928691793: "Fresh",
    1337905799061700709: "Regular",
    1337905891667742770: "Organizer",
}

# Auto-assigned class/role Discord roles, granted on approval based on the
# applicant's selected main role and class (in addition to Fresh). Maps our
# internal role/class name -> Discord role ID. Uses the same names as ROLES
# and CLASS_SPECS' keys above, so icons resolve automatically via
# icons.resolve_role_icon / icons.resolve_class_icon.
AUTO_ROLE_IDS = {
    "Tank": 1526338687087022151,
    "Healer": 1526341457173020888,
    "DPS": 1526341502018523266,
    "Mage": 1526341727634325584,
    "Warrior": 1526341753022709860,
    "Shaman": 1526341770328150066,
    "Hunter": 1526341788242149436,
    "Warlock": 1526341809318400172,
    "Paladin": 1526341858966507631,
    "Druid": 1526341878457565355,
    "Priest": 1526341889723207911,
    "Rogue": 1526342262488043560,
}

# Archive: /gearcheck archive moves every approved/denied application out
# of the review channel into a separate archive channel (re-posted there,
# original deleted). Set ARCHIVE_ENABLED to False to disable the command
# entirely without needing to remove the channel ID.
ARCHIVE_ENABLED = True
ARCHIVE_CHANNEL_ID = 1337906439066488914

# --- Attendance tracking (Regular-role eligibility) ---
# Regular role - separate from FRESH_EXEMPT_ROLE_IDS above (that list is
# about skipping the Fresh assignment/nickname on approval; this is the
# specific ID the attendance feature checks/toggles).
REGULAR_ROLE_ID = 1337905799061700709

# Organizer role - same ID as FRESH_EXEMPT_ROLE_IDS' "Organizer" entry above,
# given its own named constant since cogs/raid_logs.py gates raid-log
# tagging (Main/Alt/Other/Reset) to this role specifically, separate from
# the broader MOD_ROLE_ID gate (which can Summarize/refresh but not tag).
ORGANIZER_ROLE_ID = 1337905891667742770

# --- Raid log tagging (cogs/raid_logs.py) ---
# Daily cutoff ("HH:MM", 24h, Europe/Amsterdam - same TZ convention as
# raid_summary.py's AMSTERDAM_TZ) past which a tagged-but-not-yet-summarized
# Main/Alt raid log auto-completes the automatable part of Summarize (see
# RaidLogsCog._auto_summarize_loop). Main/alt raids reliably start
# 20:00-20:30 and never run past midnight, so a fixed same-day cutoff is
# safe - see the module docstring for why this isn't WCL's report.endTime
# or a boss-kill trigger instead (both look identical to "raid over" on a
# report that's just paused between two nights of the same raid).
RAID_LOG_AUTO_SUMMARIZE_TIME = "23:59"

# How close together (in minutes) two "started a new report" posts for the
# same zone have to be to be treated as simultaneous duplicate live-logs
# (multiple people starting a log for the same raid at once) rather than two
# genuinely different logs - see RaidLogsCog._find_duplicate_entry.
RAID_LOG_DUPLICATE_WINDOW_MINUTES = 20

ATTENDANCE_WINDOW = 5           # how many of the most recent tagged main-raid
                                # logs count toward eligibility
ATTENDANCE_MIN_ATTENDED = 3     # out of ATTENDANCE_WINDOW, needed to be
                                # Regular-eligible (or keep Regular status)
ATTENDANCE_MIN_KILLS_PER_LOG = 1  # boss kills needed within a single log's
                                   # roster to count as "attended" that week
ATTENDANCE_INCLUDE_BASELINE = 4   # assumed attended count (out of
                                   # ATTENDANCE_WINDOW) applied for one
                                   # /checkattendance run after re-including
                                   # someone who was excused

ROSTER_FRESH_ACTIVITY_WINDOW = 5  # the Raider Roster message only lists
                                    # Fresh members active in at least one of
                                    # this many recent logs (Regular members
                                    # are always shown regardless) - keeps
                                    # the roster usable at large Fresh counts

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


# --- Raid summary feature (cogs/raid_summary.py) ---

# A parse at or above this percentile gets called out by name in the
# summary's "elite parses" section.
PARSE_HIGHLIGHT_THRESHOLD = 99

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


# Banner image shown below the title of each raid summary, keyed by the
# same tier "name" string used in CURRENT_TIER/PREVIOUS_TIER above. Value is
# a path (relative to the bot's working directory) to a local image file -
# it's attached to the summary's first message and referenced via Discord's
# attachment:// scheme, since Components V2 has no plain "image URL" field.
# A plain http(s):// URL also works here if you'd rather host it externally.
# A tier with a missing/unreadable file just posts without a banner (never
# blocks the summary) - a warning is logged so it's easy to spot.
RAID_TIER_BANNERS = {
    "BT/Hyjal": "images/banner-bt.jpg",
    "SSC/TK": "images/banner-ssctk.jpg",
}

# Forum tags the raid-summary forum channel already has, auto-applied to
# every new thread. Two matching schemes are used side by side:
#   - Clear-status tags below are matched by NAME (case-insensitive)
#     against whatever tags exist in the channel - no fixed ID was given
#     for these, so create tags named exactly "Full Clear"/"Progress" in
#     the forum channel's settings.
#   - Tier and raid-type tags (TIER_TAG_IDS / RAID_TYPE_TAG_IDS further
#     down) are matched by exact Discord tag ID instead - faster/more
#     robust than name-matching, but means those ID constants need
#     updating if the tags are ever deleted and recreated in Discord.
# Either way, the bot never creates/edits forum tags itself (that's server
# structure, left to a moderator) - a tag that doesn't exist/match is
# silently skipped rather than blocking the post.
CLEAR_STATUS_TAG_NAMES = {
    "full_clear": "Full Clear",
    "progress": "Progress",
}

# Tier -> forum tag ID (see the note above CLEAR_STATUS_TAG_NAMES).
TIER_TAG_IDS = {
    "BT/Hyjal": 1542313259779231815,
    "SSC/TK": 1542313289575432212,
}

# Main-raid vs. alt/fun-raid -> forum tag ID - which one applies is a
# moderator's call per raid (there's no way to infer it from WCL data), so
# /raidsummary's `raid_type` option picks between these two keys.
RAID_TYPE_TAG_IDS = {
    "main": 1542313205521580082,
    "alt": 1542313324262334474,
}

# Some tiers bundle two real WoW raid instances under one WCL zone/one
# CURRENT_TIER/PREVIOUS_TIER entry (e.g. "BT/Hyjal" is actually Black Temple
# + Mount Hyjal, two separate raid entries in-game). For the raid summary's
# per-instance clear-time tracking (so e.g. Black Temple and Mount Hyjal
# clear times are tracked/compared separately, matching how WarcraftLogs
# itself breaks them out), map tier name -> {instance display name: [boss
# display names]} using the EXACT same boss name strings as that tier's
# `bosses` dict above. A tier with no entry here just gets treated as one
# single instance (using the tier's own name) - standard TBC raid
# structure, not guessed.
TIER_SUB_INSTANCES = {
    "BT/Hyjal": {
        "Black Temple": [
            "High Warlord Naj'entus", "Supremus", "Shade of Akama", "Teron Gorefiend",
            "Gurtogg Bloodboil", "Reliquary of Souls", "Mother Shahraz",
            "The Illidari Council", "Illidan Stormrage",
        ],
        "Mount Hyjal": ["Rage Winterchill", "Anetheron", "Kaz'rogal", "Azgalor", "Archimonde"],
    },
    "SSC/TK": {
        "Serpentshrine Cavern": [
            "Hydross the Unstable", "The Lurker Below", "Leotheras the Blind",
            "Fathom-Lord Karathress", "Morogrim Tidewalker", "Lady Vashj",
        ],
        "The Eye": ["Al'ar", "Void Reaver", "High Astromancer Solarian", "Kael'thas Sunstrider"],
    },
}


# --- Raid tier config ---
# `bosses` maps a display name -> WCL encounter ID.

CURRENT_TIER = {
    "name": "BT/Hyjal",
    "zone_id": 1011,
    "bosses": {
        "High Warlord Naj'entus": 601,
        "Supremus": 602,
        "Shade of Akama": 603,
        "Teron Gorefiend": 604,
        "Gurtogg Bloodboil": 605,
        "Reliquary of Souls": 606,
        "Mother Shahraz": 607,
        "The Illidari Council": 608,
        "Illidan Stormrage": 609,
        "Rage Winterchill": 618,
        "Anetheron": 619,
        "Kaz'rogal": 620,
        "Azgalor": 621,
        "Archimonde": 622,
    },
}

PREVIOUS_TIER = {
    "name": "SSC/TK",
    "zone_id": 1056,
    "bosses": {
        "Hydross the Unstable": 100623,
        "The Lurker Below": 100624,
        "Leotheras the Blind": 100625,
        "Fathom-Lord Karathress": 100626,
        "Morogrim Tidewalker": 100627,
        "Lady Vashj": 100628,
        "Al'ar": 100730,
        "Void Reaver": 100731,
        "High Astromancer Solarian": 100732,
        "Kael'thas Sunstrider": 100733,
    },
}

# If the applicant has fewer than this many full clears of the current tier
# (all current-tier bosses killed at least this many times each - see
# _count_full_clears in cogs/apply.py), also show their previous-tier
# performance for context.
NEW_TIER_LOG_THRESHOLD = 3

# --- Future tiers, pre-filled from the same debug_zones.py run so the
# eventual switch is copy/paste rather than a re-run. To advance a tier:
#   PREVIOUS_TIER = CURRENT_TIER
#   CURRENT_TIER = FUTURE_TIERS["BT/Hyjal"]
# (then do the same again for each subsequent tier as your guild progresses)
FUTURE_TIERS = {
    "Sunwell Plateau": {
        "name": "Sunwell Plateau",
        "zone_id": 1013,
        "bosses": {
            "Kalecgos": 724,
            "Brutallus": 725,
            "Felmyst": 726,
            "Eredar Twins": 727,
            "M'uru": 728,
            "Kil'jaeden": 729,
        },
    },
    # --- Wrath of the Lich King (next expansion) ---
    "Naxxramas/Sartharion/Malygos": {
        "name": "Naxx/Sarth/Maly",
        "zone_id": 1015,
        "bosses": {
            "Anub'Rekhan": 101107,
            "Grand Widow Faerlina": 101110,
            "Gluth": 101108,
            "Gothik the Harvester": 101109,
            "Instructor Razuvious": 101113,
            "Patchwerk": 101118,
            "Grobbulus": 101111,
            "Thaddius": 101120,
            "Noth the Plaguebringer": 101117,
            "Heigan the Unclean": 101112,
            "Loatheb": 101115,
            "Maexxna": 101116,
            "The Four Horsemen": 101121,
            "Sapphiron": 101119,
            "Kel'Thuzad": 101114,
            "Sartharion": 742,
            "Malygos": 734,
        },
    },
    "Vault of Archavon": {
        "name": "Vault of Archavon",
        "zone_id": 1016,
        "bosses": {
            "Archavon the Stone Watcher": 772,
            "Emalon the Storm Watcher": 774,
            "Koralon the Flame Watcher": 776,
            "Toravon the Ice Watcher": 885,
        },
    },
    "Ulduar": {
        "name": "Ulduar",
        "zone_id": 1017,
        "bosses": {
            "Flame Leviathan": 744,
            "Ignis the Furnace Master": 745,
            "Razorscale": 746,
            "XT-002 Deconstructor": 747,
            "The Iron Council": 748,
            "Kologarn": 749,
            "Auriaya": 750,
            "Hodir": 751,
            "Thorim": 752,
            "Freya": 753,
            "Mimiron": 754,
            "General Vezax": 755,
            "Yogg-Saron": 756,
            "Algalon the Observer": 757,
        },
    },
    "Trial of the Crusader": {
        "name": "Trial of the Crusader",
        "zone_id": 1018,
        "bosses": {
            "Northrend Beasts": 629,
            "Lord Jaraxxus": 633,
            "Faction Champions": 637,
            "Val'kyr Twins": 641,
            "Anub'arak": 645,
        },
    },
    "Onyxia": {
        "name": "Onyxia",
        "zone_id": 1019,
        "bosses": {
            "Onyxia": 101084,
        },
    },
    "Icecrown Citadel": {
        "name": "Icecrown Citadel",
        "zone_id": 1020,
        "bosses": {
            "Lord Marrowgar": 845,
            "Lady Deathwhisper": 846,
            "Icecrown Gunship Battle": 847,
            "Deathbringer Saurfang": 848,
            "Festergut": 849,
            "Rotface": 850,
            "Professor Putricide": 851,
            "Blood Council": 852,
            "Queen Lana'thel": 853,
            "Valithria Dreamwalker": 854,
            "Sindragosa": 855,
            "The Lich King": 856,
        },
    },
    "Ruby Sanctum": {
        "name": "Ruby Sanctum",
        "zone_id": 1021,
        "bosses": {
            "Halion": 887,
        },
    },
}