"""
Config constants: everything that's actually tuned per-deployment - feature
toggles, Discord role/channel/tag IDs, icon sourcing, attendance/raid-log
tuning knobs, tracked abilities, and the current/previous raid tier
definitions. See game_data.py for static WoW reference data that's the same
for every deployment and never needs touching here.

TIER TRANSITION: when Sunwell Plateau releases, update CURRENT_TIER below to
FUTURE_TIERS["Sunwell Plateau"], and set PREVIOUS_TIER to today's
CURRENT_TIER value (BT/Hyjal). That's the one-line switch.

All zone/encounter IDs below were meant to be pulled directly from WCL's
`worldData.zones` query against the fresh.warcraftlogs.com host, not
guessed - but CURRENT_TIER's (BT/Hyjal) values from that were simply wrong
(every boss ID off by exactly 50000, zone_id off entirely - confirmed and
corrected via /raidsummary-refresh-report's diagnostic breakdown against a
real report, see CURRENT_TIER's own comment for how this was found: it's
why boss kills never showed up in a raid summary for the whole time this
tier's numbers were wrong). Treat any not-yet-live tier's IDs as this
file's INTENT, not a guarantee - FUTURE_TIERS' Sunwell Plateau entry
has the same suspicious shape and is flagged unverified for exactly this
reason.
"""

import os as _os


def _env_int(name: str) -> int:
    value = (_os.environ.get(name) or "").strip()
    return int(value) if value.isdigit() else 0


# --- Guild identity (webapi/branding.py) ---
# The rulesets (Blizzard ids: 1 PvP, 2 Normal, 3 Roleplay, 15 Roleplay PvP)
# the guild plays on. Character lookups and applications flag characters on
# another ruleset. Empty: any ruleset.
CHARACTER_RULESETS = []


# The guild's name as the dashboard shows it (header, login page). Empty:
# the Discord server's name. The logo is uploaded in the dashboard and kept
# next to the database, not here.
GUILD_DISPLAY_NAME = ""


# --- Discord server: channels, roles and realm ---
# Set these in the web dashboard (Settings, or the setup guide). The values
# below are only the fallback: whatever .env has for the same name (how
# they were configured before the dashboard existed), else 0 / empty =
# "not set". A feature whose channel isn't set yet just stays inactive
# instead of stopping the bot from starting.

# Role allowed to use moderator commands, buttons and the web dashboard
# (anyone with the Manage Roles permission counts as a moderator too).
MOD_ROLE_ID = _env_int("MOD_ROLE_ID")

# Role given to an approved applicant (your "Fresh" role).
FRESH_ROLE_ID = _env_int("FRESH_ROLE_ID")

# Channel where applications are posted for review (Approve/Deny buttons).
REVIEW_CHANNEL_ID = _env_int("REVIEW_CHANNEL_ID")

# Channel where new members apply. The bot posts and pins a short "how to
# apply" explainer with the Start button here. Optional.
GEAR_CHECK_CHANNEL_ID = _env_int("GEAR_CHECK_CHANNEL_ID")

# Moderator-only channel where announcement and tier-recap drafts are
# posted for editing before they're published. Needed for /announce.
ANNOUNCEMENT_SANDBOX_CHANNEL_ID = _env_int("ANNOUNCEMENT_SANDBOX_CHANNEL_ID")

# Channel for the pinned attendance messages (log list, roster, overview).
ATTENDANCE_CHANNEL_ID = _env_int("ATTENDANCE_CHANNEL_ID")

# General moderator chat. Optional: when set, attendance overview
# refreshes are mirrored here too, since the attendance channel is easy to
# miss.
MODERATOR_CHANNEL_ID = _env_int("MODERATOR_CHANNEL_ID")

# FORUM channel where /raidsummary posts each raid's recap as a new thread.
# Create the tier and clear-status tags in the forum's settings first.
RAID_SUMMARY_FORUM_CHANNEL_ID = _env_int("RAID_SUMMARY_FORUM_CHANNEL_ID")

# Channel where your WCL report webhook (e.g. "Crusader's Logs") posts new
# reports. Optional: enables /raidsummary's report picker and the raid log
# automation. Usually hidden from members.
RAID_LOGS_CHANNEL_ID = _env_int("RAID_LOGS_CHANNEL_ID")

# Channel the bot posts its cleaned-up, taggable copy of each new report
# to. Optional: leave unset to turn the raid log tagging automation off.
RAID_LOGS_REPOST_CHANNEL_ID = _env_int("RAID_LOGS_REPOST_CHANNEL_ID")

# Your realm as WarcraftLogs writes it in character URLs, e.g. "spineshatter"
# for fresh.warcraftlogs.com/character/eu/spineshatter/Name.
SERVER_SLUG = _os.environ.get("SERVER_SLUG", "")

# Region code: us, eu, kr, tw or cn.
SERVER_REGION = _os.environ.get("SERVER_REGION", "us")

# Your guild's exact name on WarcraftLogs. Optional: enables the guild-rank
# section of raid summaries.
GUILD_NAME = _os.environ.get("GUILD_NAME", "")


# --- Logging ---
# Controls how chatty the bot's OWN console logging is (every logger named
# "wow-apply-bot" or "wow-apply-bot.<cog>" - see each file's `log =
# logging.getLogger(...)` line). One of Python's standard level names:
# "DEBUG", "INFO" (default), "WARNING", "ERROR". DEBUG adds low-level
# tracing (API token fetch/reuse, per-lookup cache hit/miss) to the
# WarcraftLogs/Blizzard/Wowhead clients - useful when chasing a lookup bug,
# noisy for normal operation.
LOG_LEVEL = "INFO"

# discord.py's OWN internal logger (gateway connects/reconnects, heartbeats,
# rate-limit waits) is controlled separately from LOG_LEVEL above - DEBUG
# there is extremely verbose (a line or more per heartbeat) and almost never
# what you actually want even when debugging THIS bot's own code, so it
# defaults to WARNING regardless of LOG_LEVEL. Set it to "INFO" or "DEBUG"
# yourself only when actually diagnosing a gateway/connection-level issue.
LOG_DISCORD_LIBRARY_LEVEL = "WARNING"

# --- Feature toggles: which optional cogs load at startup ---
# /apply (cogs/apply.py) is the bot's core feature and always loads - every
# flag below controls one other, independently-disableable cog. Flip one to
# False and restart to drop that feature (and its commands) entirely for a
# deployment that doesn't want it, without editing bot.py's EXTENSIONS list
# or deleting any code - see bot.py's own comment above EXTENSIONS.
#
# Two dependencies to know about, both handled gracefully rather than
# crashing if broken (see each cog's own bot.get_cog() calls), but worth
# getting right anyway: raid_logs.py reuses raid_summary.py's UI directly
# and attendance.py's addlog/refresh methods (its whole reason to exist -
# see cogs/raid_logs.py's module docstring), and tier_retrospective.py
# reads raid_summary.py's cached per-report data (see its own module
# docstring). Disabling RAID_SUMMARY while RAID_LOGS or TIER_RETROSPECTIVE
# stays enabled leaves that cog loaded but unable to do its actual job -
# bot.py logs a warning at startup if it spots that combination.
FEATURE_ANNOUNCEMENTS_ENABLED = True
FEATURE_EMOJI_ADMIN_ENABLED = True
FEATURE_ATTENDANCE_ENABLED = True
FEATURE_RAID_SUMMARY_ENABLED = True
FEATURE_RAID_LOGS_ENABLED = True
FEATURE_TIER_RETROSPECTIVE_ENABLED = True
FEATURE_POLLS_ENABLED = True
# cogs/audit_log.py: records who ran which command / clicked which button
# in Discord, for the dashboard's Audit log page. (Dashboard actions are always recorded while the dashboard runs.)
FEATURE_AUDIT_LOG_ENABLED = True
# cogs/notifications.py: posts alerts (bot restarts and updates, unexpected
# stops, errors, dashboard changes) to a moderator channel - see the
# "Notifications" settings below.
FEATURE_NOTIFICATIONS_ENABLED = True
# cogs/roster.py: the guild roster - in-game characters (read from
# Blizzard) linked to Discord members (/character).
FEATURE_ROSTER_ENABLED = True
# cogs/leveling.py: leveling leaderboard and level milestone announcements.
# Needs the roster.
FEATURE_LEVELING_ENABLED = True
# cogs/crafting.py: the crafting channel (who in the guild can make what,
# and the featured recipes picked on the dashboard). Needs the roster.
FEATURE_CRAFTING_ENABLED = True
# cogs/auction_prices.py: auction house prices from Blizzard (the Auction
# prices page) and answers to "price [Item]" in the guild chat channel.
FEATURE_AUCTION_PRICES_ENABLED = True

# cogs/web_dashboard.py: the browser-based management dashboard (see the
# README's "Web dashboard" section). Off by default - turn it on in
# config/deployment_local.py on the machine that should serve it, along
# with the WEB_* / DISCORD_CLIENT_SECRET values in .env. Can't be switched
# off from the dashboard itself (that would lock everyone out of it).
#
# Can also be switched on with WEB_DASHBOARD_ENABLED=true in the
# environment (that's how the Docker setup in deploy/ does it).
FEATURE_WEB_DASHBOARD_ENABLED = _os.environ.get("WEB_DASHBOARD_ENABLED", "").strip().lower() in ("1", "true", "yes")

# --- Audit log (audit.py, cogs/audit_log.py) ---
# Every action is stored in the database (crusader.db) for the dashboard's
# Audit log page, and also written as one line to a
# plain-text file for reading on the server. The file path is relative to
# wherever the bot is started from, same as applications.json etc.
AUDIT_LOG_PATH = "audit.log"

# How many days of audit history the database keeps. Older entries are
# deleted automatically. 0 keeps everything forever.
AUDIT_LOG_RETENTION_DAYS = 365

# The plain-text file rotates once it reaches this many bytes, keeping
# AUDIT_LOG_BACKUP_COUNT older files (audit.log.1, audit.log.2, ...).
AUDIT_LOG_MAX_BYTES = 5 * 1024 * 1024
AUDIT_LOG_BACKUP_COUNT = 5

# --- System log (system_log.py) ---
# Which console output is also kept in the dashboard's System log, so it
# can be read without SSH access: "ERROR" (errors only), "WARNING" (default:
# warnings and errors), "INFO" (everything the console shows, noisy) or
# "OFF". Bot starts, stops, updates and unexpected stops are always kept.
SYSTEM_LOG_LEVEL = "WARNING"

# How many days of system log the database keeps. 0 keeps everything.
SYSTEM_LOG_RETENTION_DAYS = 90

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

# --- Game version and Blizzard API (wow_api.py) ---
# Which game the roster and Blizzard lookups are for. Characters are stored
# per game, so switching keeps the old characters apart.
GAME_VERSION = "forever"

# Blizzard API namespaces for that game (the region is added automatically,
# e.g. "static-forever" becomes "static-forever-eu"). Static = game data
# (classes, items), dynamic = rulesets and auction houses (Forever has no
# realms), profile = characters and accounts.
BLIZZARD_STATIC_NAMESPACE = "static-forever"
BLIZZARD_DYNAMIC_NAMESPACE = "dynamic-forever"
BLIZZARD_PROFILE_NAMESPACE = "profile-forever"

# Language of names returned by Blizzard's API.
BLIZZARD_LOCALE = "en_GB"

# How to look a character up by name, tried in order until one answers.
# {name} and {surname} are filled in (Forever names are unique per region,
# so there's no realm). Characters can be looked up from level 10; the
# API explorer (Maintenance) shows what Blizzard answers.
CHARACTER_LOOKUP_PATHS = ["/profile/wow/character/{name}-{surname}"]

# Where item names link to (application cards, Applications and Character
# lookup pages). {id} is the item's ID for the game version. Empty: names
# aren't links. Wowhead's WoW Forever pages are under /forever/.
ITEM_LINK_URL = "https://www.wowhead.com/forever/item={id}"

# Same for spells (e.g. on the dashboard's Emoji page). {id} is the spell's ID.
SPELL_LINK_URL = "https://www.wowhead.com/forever/spell={id}"

# Show Wowhead's tooltip when hovering an item or spell link on the
# dashboard (Wowhead's tooltip script, loaded from wow.zamimg.com). It only
# works for links to wowhead.com. Off: plain links, no third-party script.
WOWHEAD_TOOLTIPS_ENABLED = True

# --- Guild roster (roster.py, cogs/roster.py) ---
# The guild's in-game name, as Blizzard's API knows it (e.g. "Encore").
# Its roster (every member and their level) is read from Blizzard. Empty:
# only characters members link themselves are tracked.
GUILD_ROSTER_GUILD = ""

# How often (minutes) the guild roster is read from Blizzard. Blizzard
# itself refreshes it every few hours, so more often gains little.
GUILD_ROSTER_REFRESH_MINUTES = 60

# Members may link their own characters with /character. Off: only
# moderators link characters (in Discord or on the dashboard).
GUILD_ROSTER_SELF_SERVICE = True

# The posted roster lists characters from this level, plus every member's
# main whatever its level.
GUILD_ROSTER_POST_MIN_LEVEL = 10

# Channel where the bot keeps the guild roster posted (class icon, level,
# character, Discord member), edited whenever it changes. Best a channel
# only the bot can post in. Empty: not posted.
GUILD_ROSTER_CHANNEL_ID = 0

# Show each character's primary professions on the posted roster (icon and
# skill/maximum, e.g. Enchanting 200/225). Read from Blizzard after the
# player logs out.
GUILD_ROSTER_POST_PROFESSIONS = True

# --- Crafting (crafting.py, cogs/crafting.py) ---
# Channel where the bot keeps the guild's crafting posted: every
# profession with its crafters, and the featured recipes (picked on the
# dashboard's Crafting page) with who can make them and the materials.
# Best a channel only the bot can post in. Empty: not posted.
CRAFTING_CHANNEL_ID = 0

# List every profession and its crafters (off: only the featured recipes).
CRAFTING_SHOW_PROFESSIONS = True

# At most this many crafters per profession in the channel (highest skill
# first); the dashboard shows them all.
CRAFTING_CRAFTERS_PER_PROFESSION = 10

# Leave featured recipes nobody in the guild knows out of the channel (they
# stay on the Crafting page, marked as hidden).
CRAFTING_HIDE_UNKNOWN_RECIPES = False

# --- Auction prices (auction_prices.py, cogs/auction_prices.py) ---
# Settings starting with AUCTION_ apply immediately (webapi/settings.py).
# The guild's auction house: a ruleset (WoW Forever has no realms; 1 is
# PvP) and one of its houses. Every ruleset has several houses per faction
# with the same name; moderators pick theirs on the Auction prices page,
# which can compare the houses. 0: none picked, no prices.
AUCTION_RULESET_ID = 1
AUCTION_HOUSE_ID = 0

# How often (minutes) to ask Blizzard for a newer snapshot. An unchanged
# one costs a tiny "not modified" answer; Blizzard renews them about hourly.
AUCTION_REFRESH_MINUTES = 15

# The guild chat channel (the one linked to the in-game guild chat) where
# the bot answers "price [Item]". 0: it doesn't answer anywhere.
AUCTION_PRICE_CHANNEL_ID = 0

# Phrases that ask for a price when they come right before an [Item]
# ("price [Linen Cloth]", "what is the price of [Linen Cloth]"); case
# doesn't matter, "of"/"on" after them is allowed.
AUCTION_PRICE_TRIGGERS = ["price", "price check", "pc", "what is the price of", "what's the price of",
                          "how much is", "how much are"]

# Where the answer goes: "dm" (a direct message to whoever asked; everyone
# posting in the linked guild chat channel has linked their account, so
# it's their own Discord account) or "channel" (a reply in the answer
# channel below).
AUCTION_PRICE_ANSWER_IN = "dm"

# The channel for "channel" answers (and failed DMs), e.g. a #price-check
# channel; the asker is mentioned. 0: the guild chat channel itself
# (where Blizzard's link may not show the bot's messages in the game).
AUCTION_PRICE_ANSWER_CHANNEL_ID = 0

# When a DM can't be delivered (the player doesn't accept DMs from server
# members), answer in the channel instead.
AUCTION_PRICE_DM_FALLBACK = True

# Also answer when the phrase comes right after the item(s), at the end of
# the message ("[Iron Bar] price?").
AUCTION_PRICE_TRIGGER_AFTER_ITEM = True

# The same item is answered at most once per this many seconds per
# channel, so a burst of questions doesn't flood the chat.
AUCTION_PRICE_COOLDOWN_SECONDS = 30

# --- Leveling (cogs/leveling.py) ---
# Channel where level milestones are announced. Empty: no announcements
# (the leaderboard still works).
LEVELING_CHANNEL_ID = 0

# Levels that get an announcement when a character reaches them.
LEVELING_MILESTONES = [10, 20, 30, 40, 50, 60]

# Also announce milestones for alts (off: only mains, and characters not
# linked to a member yet).
LEVELING_ANNOUNCE_ALTS = False

# How often (minutes) the bot checks each character's profile for a new
# level. Blizzard updates a profile when the player logs out, so this is
# how soon after logging out a level-up is noticed. Characters below level
# 10 have no profile: their levels come from the guild roster.
LEVELING_REFRESH_MINUTES = 5

# --- Notifications (cogs/notifications.py) ---
# Channel the bot posts its notifications in. Leave it empty to use the
# moderator chat channel (Discord server settings).
NOTIFICATIONS_CHANNEL_ID = 0

# Say when the bot is back online after a restart or update (which version,
# why it stopped, how long it was down), when it stopped unexpectedly, and
# when its Discord connection was down for a while.
NOTIFY_BOT_STATUS = True

# Report errors from the bot's console: how many and where they came from
# (the details stay in the dashboard's System log, which only admins see).
NOTIFY_ERRORS = True

# At most one error report per this many minutes; errors in between are
# counted into the next report.
NOTIFY_ERRORS_EVERY_MINUTES = 15

# Report changes made on the dashboard: settings, API keys, setup, restarts
# and bot message edits. Several changes in a row become one message.
NOTIFY_DASHBOARD_CHANGES = True

# Report announcements published, edited or deleted from the dashboard.
NOTIFY_ANNOUNCEMENTS = True

# --- Emoji library (emoji_library.py) ---
# Where item and spell icons for the bot's emoji are looked up: Blizzard's
# Game Data API namespaces, tried in this order (the region is added
# automatically). "static-classic" = progression Classic (TBC Anniversary),
# "static" = the modern game, "static-classic1x" = Classic Era. Put the
# namespace of the game version the guild plays first.
EMOJI_BLIZZARD_NAMESPACES = ["static-forever", "static-classic", "static"]

# Ask Wowhead's tooltip service when Blizzard can't help: for items/spells
# Blizzard has no record of (e.g. spells removed from the modern game), and
# for the icon's name when Blizzard only gives a file number (used to name
# emoji and to spot icons shared with Wowhead-only spells). Blizzard is
# always tried first and its images are used whenever it has them.
EMOJI_WOWHEAD_FALLBACK = True

# The game version Wowhead lookups use (Wowhead's "dataEnv"): 5 = TBC
# Classic, 4 = Classic Era, 1 = the modern game. Only used by the fallback.
EMOJI_WOWHEAD_DATA_ENV = 5

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

# Key format: "ClassName:SpecName", the spec as Blizzard names it for the game
# version (WoW Forever names some trees differently, e.g. "Shadow Magic").
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
    "Priest:Shadow Magic": "https://wow.zamimg.com/images/wow/icons/large/spell_shadow_shadowwordpain.jpg",  # Forever

    # Shaman
    "Shaman:Elemental":    "https://wow.zamimg.com/images/wow/icons/large/spell_nature_lightning.jpg",
    "Shaman:Elemental Combat": "https://wow.zamimg.com/images/wow/icons/large/spell_nature_lightning.jpg",  # Forever
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
    # Key must match CLASS_SPECS' "Feral Combat" exactly - a mismatched key
    # here (e.g. "Druid:Feral") silently never resolves anywhere it's used.
    "Druid:Feral Combat":  "https://wow.zamimg.com/images/wow/icons/large/ability_racial_bearform.jpg",
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
# internal role/class name -> Discord role ID. Uses the same names as
# game_data.ROLES and game_data.CLASS_SPECS' keys, so icons resolve
# automatically via icons.resolve_role_icon / icons.resolve_class_icon.
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

# Archive: /applications archive (and the dashboard's Archive button) moves
# every approved/denied application out of the review channel into a
# separate archive channel (re-posted there, original deleted). Set ARCHIVE_ENABLED to False to disable the command
# entirely without needing to remove the channel ID.
ARCHIVE_ENABLED = True
ARCHIVE_CHANNEL_ID = 1337906439066488914

# Members with this role can Approve/Deny/Reset applications on the review
# card and on the dashboard without being moderators; moderators always can
# (ApplyCog.can_decide, webapi/applications_api.py). Lets REVIEW_CHANNEL_ID
# be opened up read-only to a wider audience (e.g. Regulars) without
# letting them act on applications. This is a code-level check, not a
# Discord permission: Discord has no native permission that blocks clicking
# a message component separately from viewing it, so anyone who can see the
# review channel can technically click the button - this check is what
# actually stops them.
APPROVER_ROLE_ID = 1482653670855413822

# Gates one-time tools that are noise in the command list day-to-day:
# /raidsummary-bulk (a one-shot import of old reports). Off by default.
# Raw Blizzard answers are in the dashboard's Blizzard API explorer.
DEBUG_COMMANDS_ENABLED = False

# --- Gear check (gear.py) ---
# The gear summary on an application (and /gearcheck character) comes from
# Blizzard's API: gear, enchants, gems, talents and item level as of the
# character's last logout.
#
# Minimum character level and equipped item level for applicants. Shown on
# the review card as met or not; 0 = no minimum.
GEARCHECK_MIN_LEVEL = 0
GEARCHECK_MIN_ITEM_LEVEL = 0

# Slots a geared character fills: an empty one is flagged. Blizzard's slot
# names (HEAD, NECK, SHOULDER, BACK, CHEST, WRIST, HANDS, WAIST, LEGS,
# FEET, FINGER_1, FINGER_2, TRINKET_1, TRINKET_2, MAIN_HAND, OFF_HAND,
# RANGED).
GEARCHECK_EXPECTED_SLOTS = ["HEAD", "NECK", "SHOULDER", "BACK", "CHEST", "WRIST", "HANDS", "WAIST", "LEGS",
                            "FEET", "FINGER_1", "FINGER_2", "TRINKET_1", "TRINKET_2", "MAIN_HAND"]

# Slots that should carry an enchant: one without is flagged. Depends on the
# game version's enchanting, hence a setting.
GEARCHECK_ENCHANT_SLOTS = ["HEAD", "SHOULDER", "BACK", "CHEST", "WRIST", "HANDS", "LEGS", "FEET", "MAIN_HAND"]

# Link to a character's profile page on the review card, with {region} and
# {slug} (e.g. eggmike-thewhite) filled in. Empty: no link.
GEARCHECK_PROFILE_URL = ""

# Include WarcraftLogs parses (per boss, current and previous tier) on
# applications. Off until WarcraftLogs covers the game version the guild
# plays; the tier settings (Raid tiers) say which raids count.
APPLICATION_WARCRAFTLOGS_ENABLED = False

# Heading of the statistics section on the application card (which
# statistics: the dashboard's Applications, Card statistics). Empty: no
# heading.
APPLICATION_STATISTICS_TITLE = "Raid boss kills"

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

# --- Raid summary feature (cogs/raid_summary.py) ---

# A parse at or above this percentile gets called out by name in the
# summary's "elite parses" section.
PARSE_HIGHLIGHT_THRESHOLD = 99

# --- Raid summary: buff/debuff uptime + fun-stat ability tracking ---
# Matched against WCL's own Buffs/Debuffs/Casts tables by ability display
# NAME, not spell ID - Sunder Armor/Judgement of Wisdom/potions etc. each
# have several ranks with a different spell ID depending on which one was
# cast, while the ability's NAME stays the same across all of them. Same
# name-vs-ID reasoning CLEAR_STATUS_TAG_NAMES above uses.
#
# Debuffs tracked on the BOSS (WCL table dataType: Debuffs, hostilityType:
# Enemies) - shown with both an "all fights" (bosses+trash) and a
# "boss fights only" uptime %, plus whichever raider contributed the most
# of the boss-fight uptime. See cogs/raid_summary.py's _build_uptime_lines.
#
# The three Judgements (Wisdom/Light/the Crusader) belong here, NOT in
# TRACKED_BUFFS below, despite being granted by a PALADIN - all three are
# auras a Judgement places ON THE TARGET (the boss), which is what then
# grants mana/healing/holy-damage to whoever hits it; the aura itself is
# never on a friendly player. Listing them in TRACKED_BUFFS instead (a
# table dataType: Buffs, hostilityType: Friendlies) is a table they can
# never appear in, so "Buff/Debuff Uptime" would silently show no
# Judgement line at all - see _build_uptime_lines' docstring.
TRACKED_DEBUFFS = [
    "Sunder Armor", "Expose Armor", "Faerie Fire", "Curse of the Elements", "Curse of Recklessness",
    "Judgement of Wisdom", "Judgement of Light", "Judgement of the Crusader",
]

# Buffs tracked on a PLAYER (WCL table dataType: Buffs, hostilityType:
# Friendlies) - currently empty (see the Judgement note above
# TRACKED_DEBUFFS for why the Judgements moved out of here), kept as its
# own list/code path for any future ability that genuinely is a
# player-worn buff (e.g. a class buff/proc uptime someone wants tracked).
TRACKED_BUFFS = []

# Subset of TRACKED_DEBUFFS/TRACKED_BUFFS whose "all fights" (bosses+trash)
# uptime % isn't shown at all - just the boss-fights-only number - because
# trash uptime for these isn't a meaningful/wanted stat: the "X% bosses /
# Y% all fights" line _build_uptime_lines
# normally renders becomes just "X% bosses" for anything in this set. The
# underlying WCL fetch (wcl_client.get_report_aura_uptime) still computes
# all_pct for these same as any other tracked ability - this only affects
# what gets displayed, not what gets queried/recorded (the personal-best
# record tracked in records["buffs"] was ALREADY boss-only-scoped for
# every tracked ability, trash uptime never factored into records at all -
# see _build_uptime_lines' own docstring).
TRACKED_BOSS_ONLY_ABILITIES = {"Judgement of Wisdom", "Judgement of Light", "Judgement of the Crusader"}

# Potions counted together as one "Top potion users" leaderboard. Matched
# by SPELL ID (unlike TRACKED_DEBUFFS/TRACKED_BUFFS above), not name - a
# potion's own use-cast never appears in WCL's Casts table at all (checked
# a real 38-entry Casts table for "Sunder Armor"-style ability-name
# matching - zero hits), only the temporary BUFF
# it grants does. wcl_client.get_report_summary tracks usage via that buff
# instead (WCL table dataType: Buffs, hostilityType: Friendlies, filtered
# by this exact spell ID - filtering regroups the result to one row per
# player wearing it, whose totalUses is how many times they drank it, since
# the buff doesn't stack).
TRACKED_POTION_BUFF_SPELL_IDS = {
    "Destruction Potion": 28508,
    "Haste Potion": 28507,
}

# Item IDs for the two potions above - used only to show their real Wowhead
# icons on the "Top potion users" leaderboard (via wowhead.get_item(), the
# same lookup loot icons already use) - unrelated to the spell IDs above
# (those are the granted BUFF; these are the consumed ITEM). Confirmed
# against Wowhead's TBC Classic item pages.
TRACKED_POTION_ITEM_IDS = {
    "Destruction Potion": 22839,
    "Haste Potion": 22838,
}

# Spell IDs for TRACKED_DEBUFFS/TRACKED_BUFFS above - used ONLY to fetch each
# ability's icon via Wowhead (wowhead.get_spell()), NOT for WCL matching
# (that's by name - see the comment above TRACKED_DEBUFFS for why: WCL spell
# IDs differ per rank, but the icon is the same across ranks for all of
# these, so any correct rank's ID works fine here). Confirmed against
# Wowhead's TBC Classic spell pages.
TRACKED_ABILITY_ICON_SPELL_IDS = {
    "Sunder Armor": 24317,
    "Expose Armor": 8647,
    "Faerie Fire": 25602,
    "Curse of the Elements": 44332,
    "Curse of Recklessness": 16231,
    "Judgement of Wisdom": 27164,
    "Judgement of Light": 27163,
    # No entry for "Judgement of the Crusader" - moderator didn't give an
    # ID for this one specifically (only Wisdom/Light) - it shows with no
    # icon (graceful degradation, same as any other unconfigured ability)
    # until one's added here.
}

# Decorative icons (not tied to WCL matching at all) for the two count-based
# fun-stat leaderboards that don't map to one single tracked spell/item -
# Kick's icon for "Top Interrupters" (any class's interrupt lands there, but
# Kick is the recognizable one), Dispel Magic's for "Top Dispellers".
TOP_INTERRUPTERS_ICON_SPELL_ID = 1766
TOP_DISPELLERS_ICON_SPELL_ID = 17201

# Hardcoded emoji overrides for a handful of the icons above - format
# "<:emoji_name:emoji_id>" (Discord's own inline-emoji syntax), for an
# application emoji the moderator has ALREADY manually created (via
# the Emoji page or Discord's own app-emoji settings), bypassing
# wowhead.get_spell()/icons.ensure_spell_emoji() entirely for these keys.
# The first five exist because Wowhead's spell-icon lookup started 403ing
# under load (see wowhead.py's module docstring) and these are the ones
# that fired on every single raid-summary post (TRACKED_ABILITY_ICON_
# SPELL_IDS' non-buff entries + both TOP_*_ICON_SPELL_ID constants), so
# pre-seeding them here means those requests never happen at all. The
# three Judgements are overridden for a different reason: Blizzard's
# spell-media endpoint (via bot.blizzard, tried before falling back to
# Wowhead) returns a wrong/generic icon for them - see blizzard_client.py's
# get_spell_icon() docstring. Keyed by the exact same
# TRACKED_DEBUFFS/TRACKED_BUFFS name for the tracked-ability entries;
# TOP_INTERRUPTERS_ICON_EMOJI/TOP_DISPELLERS_ICON_EMOJI are separate plain
# constants since those two aren't part of this name-keyed dict. See
# _resolve_spell_icon/_build_uptime_lines in cogs/raid_summary.py for how
# these get checked before ever falling back to Wowhead.
TRACKED_ABILITY_ICON_EMOJI = {
    "Sunder Armor": "<:Ability_Warrior_Sunder:1543989036387926078>",
    "Expose Armor": "<:Ability_Warrior_Riposte:1543989032063869031>",
    "Faerie Fire": "<:Spell_Nature_FaerieFire:1543989030960635984>",
    "Curse of the Elements": "<:Spell_Shadow_ChillTouch:1543989029844946984>",
    "Curse of Recklessness": "<:Spell_Shadow_UnholyStrength:1543989028783657000>",
    "Judgement of Wisdom": "<:Spell_Holy_RighteousnessAura:1544102694581440654>",
    "Judgement of Light": "<:Spell_Holy_HealingAura:1544102692606189629>",
    "Judgement of the Crusader": "<:Spell_Holy_HolySmite:1544102691410812938>",
}
TOP_INTERRUPTERS_ICON_EMOJI = "<:Ability_Kick:1543989033141674157>"
TOP_DISPELLERS_ICON_EMOJI = "<:SPELL_HOLY_DISPELMAGIC:1543989034949419068>"

# --- Off-tier content excluded from EVERY WCL fetch (wcl_client.get_report_summary) ---
# Some guilds keep farming old raid content (badges/gold/legacy gear)
# alongside their current tier, in the SAME WCL report as the actual raid
# night. Left in, that content contaminates every "bosses + trash" stat
# this bot computes (damage/healing/deaths/activity/potions/interrupts/
# dispels/attendance/unique-roster) since those are all summed over EVERY
# fight in the report, not just the current tier's - most visibly, an alt
# that only tagged along for a Gruul/Magtheridon farm run showing up in
# the "unique characters this tier" count. Filtered out at the earliest
# point (get_report_summary's own fight-list construction), before any
# other code ever sees these fights, so every consumer benefits with no
# per-feature patching. Applied unconditionally (not tier-specific) since
# it's cheap insurance against the same contamination recurring in a
# future tier.
#
# Encounter IDs confirmed against fresh.warcraftlogs.com's own zone-
# rankings pages (the exact "Fresh" Classic host this bot's API calls use)
# for Gruul's Lair/Magtheridon's Lair (zone 1048) - the four "council"
# adds fought alongside High King Maulgar (Krosh Firehand, Olm the
# Summoner, Kiggler the Crazed, Blindeye the Seer) share his encounter ID,
# confirmed live against a real report - not a separate fight.
EXCLUDED_ENCOUNTER_IDS = {
    50649,  # High King Maulgar (incl. the four council adds)
    50650,  # Gruul the Dragonkiller
    50651,  # Magtheridon
}

# Trash-pull fight NAMES to exclude alongside the encounters above (trash
# fights carry no encounter_id at all, so they can only be matched by
# their own display name) - confirmed against a real report.
EXCLUDED_TRASH_FIGHT_NAMES = {
    "Lair Brute",       # Gruul's Lair trash
    "Hellfire Warder",  # Magtheridon's Lair trash
}

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

# Corrected against real WCL data via /raidsummary-refresh-report's
# diagnostic breakdown - the original values below were simply wrong:
# every boss's real encounter ID is exactly 50000 higher (e.g. High
# Warlord Naj'entus is 50601, not 601), and zone_id is 1060, not 1011.
# This is why boss kills never showed up in a raid summary for this
# tier - _group_fights_by_
# encounter/_build_boss_lines/_tier_stats all matched against these IDs
# and never found a single one in a real report's fights, and
# _build_guild_rank_block's guild-zone-rankings lookup (cogs/raid_summary.py)
# and cogs/apply.py's per-zone character gear/spec lookups were silently
# querying the wrong zone entirely. PREVIOUS_TIER (SSC/TK) below uses a
# visibly different ID shape (100xxx) and hasn't shown this symptom, so
# it's left as-is - not re-verified, but no evidence it's wrong either.
CURRENT_TIER = {
    "name": "BT/Hyjal",
    "zone_id": 1060,
    "bosses": {
        "High Warlord Naj'entus": 50601,
        "Supremus": 50602,
        "Shade of Akama": 50603,
        "Teron Gorefiend": 50604,
        "Gurtogg Bloodboil": 50605,
        "Reliquary of Souls": 50606,
        "Mother Shahraz": 50607,
        "The Illidari Council": 50608,
        "Illidan Stormrage": 50609,
        "Rage Winterchill": 50618,
        "Anetheron": 50619,
        "Kaz'rogal": 50620,
        "Azgalor": 50621,
        "Archimonde": 50622,
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

# --- Future tiers, pre-filled from an earlier WCL zone/encounter lookup so
# the eventual switch is copy/paste rather than a re-run. To advance a tier:
#   PREVIOUS_TIER = CURRENT_TIER
#   CURRENT_TIER = FUTURE_TIERS["BT/Hyjal"]
# (then do the same again for each subsequent tier as your guild progresses)
#
# WARNING: Sunwell Plateau's zone_id/boss IDs below have the EXACT same
# shape the real CURRENT_TIER (BT/Hyjal) values had before they turned out
# to be wrong by exactly 50000 (see CURRENT_TIER's comment) - small
# 3-digit boss IDs, a 4-digit zone_id, from that same earlier lookup.
# Treat these as unverified, not just "not yet re-run" - re-check them
# (e.g. /raidsummary-refresh-report's diagnostic breakdown against a real
# Sunwell report, once your guild is there) BEFORE promoting this to
# CURRENT_TIER, rather than assuming they're fine because BT/Hyjal's
# fix pattern was obvious in hindsight - don't blindly add 50000 here
# without a real report to check against.
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


# --- Polls (cogs/polls.py) ---
# Defaults for /poll create. Every one of these can be overridden per poll
# through the command's own options - these just decide what you get when
# the poll starter leaves an option blank.
#
# Whether members may add their own options isn't a constant here: it
# defaults to ON when the poll is created without any starting options
# (e.g. "New guild name?") and OFF when starting options are given
# (e.g. "Yes; No; Maybe").
#
# How many options each member may add.
POLL_DEFAULT_MAX_ADDS_PER_MEMBER = 3
# With member-added options: True = a separate "adding" phase first, and the
# poll starter opens voting later (fair to people who add late). False =
# members can add options and vote at the same time.
POLL_DEFAULT_COLLECT_FIRST = True
# How many options each member may vote for (1 = one person, one vote;
# more = approval-style "pick your favourites").
POLL_DEFAULT_MAX_VOTES_PER_MEMBER = 1
# Show running vote counts while voting is open. False hides the tally until
# the poll closes (avoids bandwagoning on an early leader).
POLL_DEFAULT_LIVE_RESULTS = True
# Show who added each option.
POLL_DEFAULT_SHOW_AUTHORS = True
# Make votes public (who voted for what). Off = anonymous - only totals.
POLL_DEFAULT_SHOW_VOTERS = False
# Let members change or withdraw their vote while voting is open.
POLL_DEFAULT_ALLOW_VOTE_CHANGE = True
# True = only moderators (MOD_ROLE_ID / Manage Roles) can run /poll create.
# The poll's creator can always manage their own poll either way.
POLL_CREATE_MOD_ONLY = True
# Small-print line shown under every poll. Set to "" to drop it.
POLL_FOOTER = (
    "The Democratic Republic of Crusaders reserves the right to ignore a "
    "majority vote and implement laws as they see fit."
)
