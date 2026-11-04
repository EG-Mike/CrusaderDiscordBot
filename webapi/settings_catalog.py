"""
What the dashboard shows for each setting in config/deployment.py: where
it goes (section and tab), its label and help text, and how to edit it.

deployment.py's comments are for developers (how a setting is used in the
code); the text here is for moderators (what it does for them). Every
setting in deployment.py must be listed here (tests/test_settings.py checks
this), either shown or hidden with a reason.

  advanced   shown only with "Show advanced settings" on: raw game data,
             fine-tuning and diagnostics most guilds never touch
  hidden     not shown at all (still read from deployment.py and
             deployment_local.py); the reason is in a comment
  widget     a friendlier editor than raw JSON: "text_list", "number_list",
             "role_map" (fixed names -> a role each), "role_set" (pick roles)
  unit       shown after a number field ("minutes", "days", ...)
  choices    [(value, label)] for a dropdown
"""

from typing import Dict, List, NamedTuple, Optional, Tuple

# (id, label) in the order of the Settings page's navigation.
SECTIONS: List[Tuple[str, str]] = [
    ("general", "General"),
    ("features", "Features"),
    ("integrations", "Integrations"),
    ("system", "System"),
]

# (id, section, label, description). Groups with a custom panel (guild,
# api_keys, setup) can have few or no settings of their own.
GROUPS: List[Tuple[str, str, str, str]] = [
    ("guild", "general", "Guild & bot", "Your guild's name and logo, and how the bot appears in Discord."),
    ("discord", "general", "Moderation", "Who counts as a moderator, and where moderators talk."),
    ("features", "features", "On / off", "Switch whole features on or off. Takes effect after a restart."),
    ("announcements", "features", "Announcements", "Writing and publishing announcements."),
    ("applications", "features", "Applications", "Applying to the guild, reviewing applications and archiving them."),
    ("attendance", "features", "Attendance", "Raid attendance and who qualifies for the Regular role."),
    ("raid_logs", "features", "Raid logs", "Reposting and tagging new WarcraftLogs reports."),
    ("raid_summary", "features", "Raid summaries", "The raid recap posted after each raid."),
    ("raid_tiers", "features", "Raid tiers", "Which raids count as the current and previous tier."),
    ("polls", "features", "Polls", "Defaults for new polls."),
    ("roster", "features", "Guild roster", "Characters linked to Discord members."),
    ("leveling", "features", "Leveling", "The leveling leaderboard and milestone announcements."),
    ("notifications", "features", "Notifications", "Alerts the bot posts for moderators. Changes apply immediately."),
    ("api_keys", "integrations", "API keys", "Keys for Blizzard and WarcraftLogs."),
    ("blizzard", "integrations", "Blizzard", "Which game Blizzard's API is asked about."),
    ("warcraftlogs", "integrations", "WarcraftLogs", "How your guild and realm are named on WarcraftLogs."),
    ("icons", "integrations", "Item & spell icons", "Where the icons for emoji come from."),
    ("logging", "system", "Logging", "What the System log keeps, and for how long."),
    ("audit", "system", "Audit log", "How long the history of who did what is kept."),
    ("setup", "system", "Setup guide", "The step-by-step guide for setting the bot up."),
]


class Entry(NamedTuple):
    group: str
    label: str
    help: str = ""
    advanced: bool = False
    widget: Optional[str] = None
    unit: Optional[str] = None
    choices: Optional[List[tuple]] = None


HIDDEN: Dict[str, str] = {
    # Turning the dashboard off from the dashboard would lock everyone out;
    # it's set by WEB_DASHBOARD_ENABLED in .env.
    "FEATURE_WEB_DASHBOARD_ENABLED": "set in .env",
    # A file path on the server; change it in deployment_local.py.
    "AUDIT_LOG_PATH": "server file path",
    # Class, role and spec icons: plumbing for the TBC-era icon emoji. The
    # roster gets class and spec icons from Blizzard instead.
    "CLASS_EMOJI_NAMES": "icon plumbing",
    "ROLE_EMOJI_NAMES": "icon plumbing",
    "CLASS_ICON_URLS": "icon plumbing",
    "ROLE_ICON_URLS": "icon plumbing",
    "SPEC_ICON_URLS": "icon plumbing",
    # Fixed emoji for raid summary headers: the Emoji library finds and
    # reuses these icons by spell ID on its own.
    "TRACKED_ABILITY_ICON_EMOJI": "emoji library does this",
    "TOP_INTERRUPTERS_ICON_EMOJI": "emoji library does this",
    "TOP_DISPELLERS_ICON_EMOJI": "emoji library does this",
}

LOG_LEVELS = [("DEBUG", "Debug (very detailed)"), ("INFO", "Normal"), ("WARNING", "Warnings and errors"),
              ("ERROR", "Errors only"), ("CRITICAL", "Critical errors only")]

CATALOG: Dict[str, Entry] = {
    # --- General -------------------------------------------------------------------
    "GUILD_DISPLAY_NAME": Entry(
        "guild", "Guild name",
        "Shown at the top of the dashboard and on the login page. Empty: the Discord server's name."),
    "SERVER_REGION": Entry(
        "guild", "Region", "The region your guild plays in. Used for Blizzard and WarcraftLogs lookups.",
        choices=[("eu", "Europe"), ("us", "Americas & Oceania"), ("kr", "Korea"), ("tw", "Taiwan"),
                 ("cn", "China")]),
    "MOD_ROLE_ID": Entry(
        "discord", "Moderator role",
        "Members with this role can use moderator commands and this dashboard. Members with the Manage Roles "
        "permission count as moderators too."),
    "MODERATOR_CHANNEL_ID": Entry(
        "discord", "Moderator channel",
        "Where moderators talk. Notifications go here unless they have their own channel, and attendance "
        "updates are mirrored here."),

    # --- Features: on / off ------------------------------------------------------------
    "FEATURE_ANNOUNCEMENTS_ENABLED": Entry("features", "Announcements", "/announce and the Announcements page."),
    "FEATURE_EMOJI_ADMIN_ENABLED": Entry("features", "Emoji", "Adding item and spell icons as emoji on the "
                                                              "Emoji page."),
    "FEATURE_POLLS_ENABLED": Entry("features", "Polls", "/poll and the Polls page."),
    "FEATURE_ROSTER_ENABLED": Entry("features", "Guild roster", "Characters linked to members: /character and the "
                                                                "Roster page."),
    "FEATURE_LEVELING_ENABLED": Entry("features", "Leveling", "Leveling leaderboard and milestone announcements. "
                                                              "Needs the guild roster."),
    "FEATURE_ATTENDANCE_ENABLED": Entry("features", "Attendance", "/checkattendance and the attendance messages."),
    "FEATURE_RAID_SUMMARY_ENABLED": Entry("features", "Raid summaries", "/raidsummary and the raid recaps."),
    "FEATURE_RAID_LOGS_ENABLED": Entry("features", "Raid logs", "Reposting and tagging new WarcraftLogs reports. "
                                                                "Needs Raid summaries and Attendance."),
    "FEATURE_TIER_RETROSPECTIVE_ENABLED": Entry("features", "Tier recap", "/tier-recap. Needs Raid summaries."),
    "FEATURE_NOTIFICATIONS_ENABLED": Entry("features", "Notifications", "Alerts for moderators about restarts, "
                                                                        "updates, errors and dashboard changes."),
    "FEATURE_AUDIT_LOG_ENABLED": Entry("features", "Audit log of Discord actions",
                                       "Records the commands, buttons and forms members use in Discord. Dashboard "
                                       "actions are always recorded."),

    # --- Announcements -------------------------------------------------------------------
    "ANNOUNCEMENT_SANDBOX_CHANNEL_ID": Entry(
        "announcements", "Drafts channel",
        "A moderator-only channel where /announce and /tier-recap post drafts to edit before publishing."),

    # --- Applications --------------------------------------------------------------------
    "GEAR_CHECK_CHANNEL_ID": Entry(
        "applications", "Apply channel",
        "Where new members apply. The bot pins a short explainer with an Apply button here."),
    "REVIEW_CHANNEL_ID": Entry(
        "applications", "Review channel", "Where applications are posted for approval."),
    "APPROVER_ROLE_ID": Entry(
        "applications", "Approver role",
        "Only members with this role can approve or deny applications, so others can read the review channel "
        "without acting on it."),
    "FRESH_ROLE_ID": Entry("applications", "Role for approved applicants", "Given when an application is approved."),
    "FRESH_EXEMPT_ROLE_IDS": Entry(
        "applications", "Roles that skip the approval role",
        "Members who already have one of these roles keep them: they don't get the approval role or a new "
        "nickname when approved again.", widget="role_set"),
    "AUTO_ROLE_IDS": Entry(
        "applications", "Class and role roles",
        "Given on approval, matching the main role and class the applicant picked.", widget="role_map"),
    "MAX_SCREENSHOTS": Entry("applications", "Gear screenshots per application",
                             "How many screenshots an applicant can attach (1 to 9)."),
    "NEW_TIER_LOG_THRESHOLD": Entry(
        "applications", "Show previous tier below",
        "If an applicant has fewer full clears of the current tier than this, their previous-tier results are "
        "shown too.", unit="clears"),
    "ARCHIVE_ENABLED": Entry("applications", "Archive command",
                             "/gearcheck archive moves decided applications to the archive channel."),
    "ARCHIVE_CHANNEL_ID": Entry("applications", "Archive channel", "Where /gearcheck archive moves them."),
    "ARMORY_BLOCK_ENABLED": Entry(
        "applications", "Show gear and talents from Blizzard",
        "Adds the character's equipped gear and talents to the review card. Off while Blizzard's character data "
        "isn't reliable for this game.", advanced=True),
    "OXM_REGISTER_COMMAND_ID": Entry(
        "applications", "OXM.gg /register command ID",
        "Turns /register in the approval message into a clickable command. Only change it if OXM.gg changes it.",
        advanced=True),

    # --- Attendance ------------------------------------------------------------------------
    "ATTENDANCE_CHANNEL_ID": Entry("attendance", "Attendance channel",
                                   "Where the attendance overview and raider list are pinned."),
    "REGULAR_ROLE_ID": Entry("attendance", "Regular role", "The role given to members who raid regularly."),
    "ORGANIZER_ROLE_ID": Entry("attendance", "Organizer role", "Members with this role can tag raid logs as main "
                                                               "or alt raids."),
    "ATTENDANCE_WINDOW": Entry("attendance", "Raids that count",
                               "How many of the most recent main raids are looked at.", unit="raids"),
    "ATTENDANCE_MIN_ATTENDED": Entry("attendance", "Raids needed for Regular",
                                     "How many of those a member must attend to become or stay Regular.",
                                     unit="raids"),
    "ATTENDANCE_MIN_KILLS_PER_LOG": Entry("attendance", "Boss kills to count as present",
                                          "How many boss kills in one raid count as attending it.", unit="kills"),
    "ATTENDANCE_INCLUDE_BASELINE": Entry(
        "attendance", "Assumed attendance after an excuse",
        "When an excused member is counted again, they're treated as having attended this many raids for one "
        "check.", unit="raids", advanced=True),
    "ROSTER_FRESH_ACTIVITY_WINDOW": Entry(
        "attendance", "Show new members active in the last",
        "The raider list only shows new (non-Regular) members who attended one of this many recent raids.",
        unit="raids", advanced=True),

    # --- Raid logs -------------------------------------------------------------------------------
    "RAID_LOGS_CHANNEL_ID": Entry("raid_logs", "WarcraftLogs channel",
                                  "Where your WarcraftLogs webhook posts new reports."),
    "RAID_LOGS_REPOST_CHANNEL_ID": Entry("raid_logs", "Repost channel",
                                         "Where the bot reposts each report with tagging buttons. Empty: no "
                                         "reposts."),
    "RAID_LOG_AUTO_SUMMARIZE_TIME": Entry(
        "raid_logs", "Finish summaries automatically at",
        "Tagged raids that weren't summarized by hand are finished at this time (24-hour clock, Amsterdam "
        "time).", unit="HH:MM"),
    "RAID_LOG_DUPLICATE_WINDOW_MINUTES": Entry(
        "raid_logs", "Treat reports as duplicates within",
        "Two reports for the same raid started this close together are treated as one.", unit="minutes",
        advanced=True),

    # --- Raid summaries ---------------------------------------------------------------------------
    "RAID_SUMMARY_FORUM_CHANNEL_ID": Entry(
        "raid_summary", "Summary forum",
        "The forum channel each raid summary is posted in, as a new post. Create the tier and clear-status tags "
        "in the forum first."),
    "PARSE_HIGHLIGHT_THRESHOLD": Entry("raid_summary", "Highlight parses from",
                                       "Parses at or above this are named in the summary.", unit="percentile"),
    "TRACKED_DEBUFFS": Entry("raid_summary", "Tracked debuffs", "Debuff uptimes shown in the summary, by name.",
                             widget="text_list"),
    "TRACKED_BUFFS": Entry("raid_summary", "Tracked buffs", "Buff uptimes shown in the summary, by name.",
                           widget="text_list"),
    "TRACKED_BOSS_ONLY_ABILITIES": Entry("raid_summary", "Boss-only uptimes",
                                         "Tracked abilities whose uptime is only shown for boss fights.",
                                         widget="text_list"),
    "CLEAR_STATUS_TAG_NAMES": Entry("raid_summary", "Clear-status forum tags",
                                    "Names of the forum tags for a full clear and for progress.", advanced=True),
    "TIER_TAG_IDS": Entry("raid_summary", "Tier forum tags", "Forum tag ID per raid tier.", advanced=True),
    "RAID_TYPE_TAG_IDS": Entry("raid_summary", "Raid type forum tags", "Forum tag ID for main and alt raids.",
                               advanced=True),
    "RAID_TIER_BANNERS": Entry("raid_summary", "Tier banners", "Banner image per raid tier (a file on the server).",
                               advanced=True),
    "TRACKED_POTION_BUFF_SPELL_IDS": Entry("raid_summary", "Potions counted",
                                           "Potion name and the spell ID of the buff it gives.", advanced=True),
    "TRACKED_POTION_ITEM_IDS": Entry("raid_summary", "Potion icons", "Potion name and item ID, for its icon.",
                                     advanced=True),
    "TRACKED_ABILITY_ICON_SPELL_IDS": Entry("raid_summary", "Ability icons",
                                            "Spell ID per tracked ability, for its icon.", advanced=True),
    "TOP_INTERRUPTERS_ICON_SPELL_ID": Entry("raid_summary", "Interrupts icon", "Spell ID whose icon heads the "
                                                                               "interrupts list.", advanced=True),
    "TOP_DISPELLERS_ICON_SPELL_ID": Entry("raid_summary", "Dispels icon", "Spell ID whose icon heads the dispels "
                                                                          "list.", advanced=True),
    "EXCLUDED_ENCOUNTER_IDS": Entry("raid_summary", "Ignored bosses",
                                    "Encounter IDs left out of every stat, e.g. old raids farmed in the same log.",
                                    widget="number_list", advanced=True),
    "EXCLUDED_TRASH_FIGHT_NAMES": Entry("raid_summary", "Ignored trash", "Trash fights left out of every stat, by "
                                                                         "name.", widget="text_list", advanced=True),

    # --- Raid tiers --------------------------------------------------------------------------------
    "CURRENT_TIER": Entry("raid_tiers", "Current tier", "Name, WarcraftLogs zone and bosses of the current tier."),
    "PREVIOUS_TIER": Entry("raid_tiers", "Previous tier", "The tier before it, for comparisons."),
    "FUTURE_TIERS": Entry("raid_tiers", "Upcoming tiers", "Ready to move into Current tier when the guild "
                                                          "moves on.", advanced=True),
    "TIER_SUB_INSTANCES": Entry("raid_tiers", "Raids within a tier",
                                "For tiers made of several raids: which bosses belong to which raid.", advanced=True),

    # --- Polls -------------------------------------------------------------------------------------------
    "POLL_CREATE_MOD_ONLY": Entry("polls", "Only moderators can start polls",
                                  "Off: everyone can use /poll create. Whoever starts a poll can always manage it."),
    "POLL_DEFAULT_MAX_VOTES_PER_MEMBER": Entry("polls", "Votes per member", "1 = one vote each; more = pick "
                                                                            "favourites.", unit="votes"),
    "POLL_DEFAULT_ALLOW_VOTE_CHANGE": Entry("polls", "Members can change their vote"),
    "POLL_DEFAULT_LIVE_RESULTS": Entry("polls", "Show results while voting",
                                       "Off: the results are revealed when the poll closes."),
    "POLL_DEFAULT_SHOW_VOTERS": Entry("polls", "Show who voted for what", "Off: only totals are shown."),
    "POLL_DEFAULT_SHOW_AUTHORS": Entry("polls", "Show who added each option"),
    "POLL_DEFAULT_COLLECT_FIRST": Entry("polls", "Collect options before voting",
                                        "When members can add options: first collect them, then open voting. Off: "
                                        "adding and voting at the same time."),
    "POLL_DEFAULT_MAX_ADDS_PER_MEMBER": Entry("polls", "Options each member can add", unit="options"),
    "POLL_FOOTER": Entry("polls", "Footer", "Small print under every poll. Empty: none."),

    # --- Guild roster -------------------------------------------------------------------------------------
    "GUILD_ROSTER_SELF_SERVICE": Entry("roster", "Members manage their own characters",
                                       "Off: only moderators (and Battle.net verification) add characters."),
    "GUILD_ROSTER_KEEP_BATTLENET_LOGIN": Entry(
        "roster", "Keep Battle.net logins for level updates",
        "After a member verifies with Battle.net, keep the (read-only, encrypted) login while it's valid, about a "
        "day, so their levels keep updating."),

    # --- Leveling ----------------------------------------------------------------------------------------
    "LEVELING_CHANNEL_ID": Entry("leveling", "Announcement channel",
                                 "Where milestones are announced. Empty: no announcements."),
    "LEVELING_MILESTONES": Entry("leveling", "Milestones", "Levels that get an announcement.",
                                 widget="number_list"),
    "LEVELING_ANNOUNCE_ALTS": Entry("leveling", "Announce alts too", "Off: mains only."),
    "LEVELING_REFRESH_MINUTES": Entry("leveling", "Check for new levels every", unit="minutes"),

    # --- Notifications ---------------------------------------------------------------------------------
    "NOTIFICATIONS_CHANNEL_ID": Entry("notifications", "Channel", "Empty: the moderator channel."),
    "NOTIFY_BOT_STATUS": Entry("notifications", "Restarts, updates and outages",
                               "When the bot is back after a restart or update, stopped unexpectedly, or lost its "
                               "connection to Discord for a while."),
    "NOTIFY_ERRORS": Entry("notifications", "Errors", "How many errors and where from. The details stay in the "
                                                      "System log."),
    "NOTIFY_ERRORS_EVERY_MINUTES": Entry("notifications", "At most one error report every", unit="minutes"),
    "NOTIFY_DASHBOARD_CHANGES": Entry("notifications", "Dashboard changes",
                                      "Settings, API keys, setup, restarts and bot message edits."),
    "NOTIFY_ANNOUNCEMENTS": Entry("notifications", "Announcements", "Published, edited or deleted from the "
                                                                    "dashboard."),

    # --- Blizzard ------------------------------------------------------------------------------------
    "GAME_VERSION": Entry("blizzard", "Game", "Characters are kept per game, so switching keeps them apart.",
                          choices=[("forever", "WoW Forever")], advanced=True),
    "BLIZZARD_LOCALE": Entry("blizzard", "Language", "Language of names from Blizzard (classes, items, realms).",
                             choices=[("en_GB", "English (UK)"), ("en_US", "English (US)"), ("de_DE", "German"),
                                      ("fr_FR", "French"), ("es_ES", "Spanish"), ("it_IT", "Italian"),
                                      ("pt_BR", "Portuguese"), ("ru_RU", "Russian")]),
    "BLIZZARD_STATIC_NAMESPACE": Entry("blizzard", "Game data namespace",
                                       "For classes, specs and items. The region is added automatically.",
                                       advanced=True),
    "BLIZZARD_DYNAMIC_NAMESPACE": Entry("blizzard", "Realm namespace", "For realms. The region is added "
                                                                       "automatically.", advanced=True),
    "BLIZZARD_PROFILE_NAMESPACE": Entry("blizzard", "Character namespace", "For characters and accounts. The region "
                                                                           "is added automatically.", advanced=True),
    "CHARACTER_LOOKUP_PATHS": Entry(
        "blizzard", "Character lookup paths",
        "Tried in order until one answers; {name}, {surname} and {realm} are filled in. Try paths in the "
        "Blizzard API explorer (Maintenance) first.", widget="text_list", advanced=True),

    # --- WarcraftLogs --------------------------------------------------------------------------------------
    "SERVER_SLUG": Entry("warcraftlogs", "Realm",
                         "Your realm as it appears in WarcraftLogs character links, e.g. \"spineshatter\"."),
    "GUILD_NAME": Entry("warcraftlogs", "Guild name", "Exactly as on WarcraftLogs. Adds the guild's ranking to "
                                                      "raid summaries."),

    # --- Item & spell icons ------------------------------------------------------------------------------
    "EMOJI_BLIZZARD_NAMESPACES": Entry(
        "icons", "Blizzard game data to search",
        "Tried in order; the region is added automatically. \"static-classic\" is TBC Anniversary, \"static\" the "
        "modern game, \"static-forever\" WoW Forever.", widget="text_list"),
    "EMOJI_WOWHEAD_FALLBACK": Entry("icons", "Ask Wowhead when Blizzard can't help",
                                    "For items and spells Blizzard doesn't know, and for icon names."),
    "EMOJI_WOWHEAD_DATA_ENV": Entry("icons", "Wowhead game version", "Which game Wowhead lookups are for.",
                                    choices=[(1, "Modern game"), (4, "Classic Era"), (5, "TBC Classic")],
                                    advanced=True),

    # --- Logging ----------------------------------------------------------------------------------------------
    "SYSTEM_LOG_LEVEL": Entry(
        "logging", "Console messages to keep",
        "Which of the bot's console messages are copied into the System log. Starts, stops and updates are "
        "always kept.",
        choices=[("OFF", "None"), ("ERROR", "Errors"), ("WARNING", "Warnings and errors"),
                 ("INFO", "Everything (noisy)")]),
    "SYSTEM_LOG_RETENTION_DAYS": Entry("logging", "Keep the System log for", "0 keeps everything.", unit="days"),
    "LOG_LEVEL": Entry("logging", "Console detail", "How much the bot writes to its console.", choices=LOG_LEVELS,
                       advanced=True),
    "LOG_DISCORD_LIBRARY_LEVEL": Entry("logging", "Discord connection detail",
                                       "How much the Discord library writes to the console. Debug is very noisy.",
                                       choices=LOG_LEVELS, advanced=True),
    "DEBUG_COMMANDS_ENABLED": Entry("logging", "One-time import commands",
                                    "/raidsummary-bulk, for importing old raid reports in one go.",
                                    advanced=True),

    # --- Audit log -------------------------------------------------------------------------------------------
    "AUDIT_LOG_RETENTION_DAYS": Entry("audit", "Keep the audit log for", "0 keeps everything.", unit="days"),
    "AUDIT_LOG_MAX_BYTES": Entry("audit", "Audit text file size",
                                 "The audit.log file on the server starts a new file at this size.", unit="bytes",
                                 advanced=True),
    "AUDIT_LOG_BACKUP_COUNT": Entry("audit", "Old audit text files to keep", unit="files", advanced=True),
}
