# WoW TBC Fresh Guild Application Bot

A Discord bot for a World of Warcraft TBC Classic ("Fresh") guild. It runs a
gear-check/application process backed by WarcraftLogs data, and a
moderator-driven announcement system.

## What the bot does

### Guild applications (gear-check)

1. A prospective member clicks a **"Start Gear-Check procedure"** button
   posted in a locked-down landing channel (or runs `/apply` directly). A
   button click only needs *View Channel* - not *Send Messages* - so this
   works even in a channel where regular members can't post, keeping it
   clutter-free.
2. A modal asks for their character's exact name. The bot looks it up on
   WarcraftLogs.
   - **Not found?** They get the option to re-enter the name (typo fix) or
     confirm it's correct and continue anyway - in which case they're asked
     to pick their class manually (no WCL data to infer it from), and the
     rest of the flow proceeds identically, just with no pre-filled
     suggestions.
3. **Found?** The bot suggests a main role/spec based on which spec they've
   logged the most kills with, then hands them a dropdown dialog (main
   role, main spec, any other specs they can play) to confirm or override.
   The role list is filtered to what their class can actually perform (a
   Priest never sees "Tank"), and "other specs" excludes whatever's
   currently picked as the main one.
4. The rest of the flow - role/spec dialog, an optional note, optional gear
   screenshot(s) - happens over **DM**, not in the public channel, so one
   applicant never sees another's note or screenshot. If DMs are closed,
   it degrades gracefully: role/spec happens ephemerally in-channel instead,
   and note/screenshot just gets skipped (still addable later, see below).
5. A summary card posts to a mod-only review channel: class/spec/role (with
   icons), guild, level, tier performance (falling back to the previous
   tier if current-tier logs are too sparse, and saying so explicitly), a
   full per-boss breakdown, any note, and any screenshots - plus a warning
   if this same person has been denied before. Reacting with ✅/❌ approves
   or denies it.
6. **Approve**: assigns the "Fresh" role, renames the applicant to their
   character name, and DMs them (including a clickable `/register` mention
   for OXM.gg's raid sign-up bot, if configured). **Deny**: DMs them and
   does nothing else.
7. A **Reset** button on every application lets a moderator undo a mistaken
   decision - it clears the reactions and puts it back to pending. The
   correction rules are deliberate: fixing a wrong denial into an approval
   still sends the acceptance DM; fixing a wrong approval into a denial
   just silently revokes the Fresh role, no DM (they were never told they
   were in, so there's nothing to walk back).
8. Applicants can add or update their note/screenshot(s) at any time - even
   after the initial flow - with **`/update-application`**, as long as
   their application is still pending.

### Announcements

1. A moderator runs **`/announce`**, fills in a modal (optional title +
   body text).
2. The bot posts a **draft** to a sandbox channel with Edit/Publish
   buttons. There's no rich-text/live-preview input surface in Discord's
   API, so this is the practical equivalent of WYSIWYG: click Edit as many
   times as needed, see the actual rendered result immediately each time.
3. **Publish** opens a native channel picker, then reconstructs the
   announcement in its final form in the chosen channel. The sandbox draft
   gets marked "Published to #channel by [mod]" - its Publish button is
   gone, but its Edit button stays, now retargeted at the published
   message.
4. The published message itself has no Edit button - only the sandbox
   draft does, so editing an announcement always happens from the
   mod-only sandbox channel, never a button visible to the whole audience
   of the published message. `/announce-edit-published <message_id>` is a
   fallback for editing one with no working button at all (e.g. one
   published before this existed); `/announce-fix-legacy-buttons` is a
   one-time cleanup that strips the old public Edit button from
   already-published announcements from before this changed - safe to run
   more than once, never touches reactions or the message's history.
5. Formatting: `## Header text 🎉` and any emoji work directly (standard
   Discord message markdown - no special handling needed). A line
   containing just `---` splits the message into separate sections, each
   with a real divider between them.

### Raid summaries

1. A moderator runs **`/raidsummary`** with no arguments at all. The bot
   immediately shows a set of dropdowns: tier, full-clear-or-progress,
   main-or-alt raid, and - when `RAID_LOGS_CHANNEL_ID` is configured (see
   section 10 below) - a **report picker** populated from recent posts in
   your #logs channel (wherever your WCL-report-posting webhook/app, e.g.
   "Crusader's Logs", announces new reports), labeled by the report's own
   title (e.g. "SSC+TK") instead of a raw link. Hitting **Continue** opens
   a **modal** for the fields that can't be a dropdown: a report-link box
   (pre-filled if one was picked above, but always editable - the dropdown
   only shows the most recent reports, so an older one still needs a
   pasted link), the Gargul loot export **pasted directly**, a note, and a
   YouTube/Twitch/image link. Two steps rather than one because Discord
   modals have no dropdown support at all - only text fields - so anything
   meant to be pick-from-a-list has to happen before the modal opens. The
   loot paste specifically has to go through the modal rather than a plain
   slash-command option - a slash-command string option is a single-line
   input in Discord's client, so pasting a multi-line export into one
   silently collapses every newline to a space and the export becomes
   unparseable. A modal's paragraph text field is the only Discord input
   that keeps real newlines intact. A normal night's export runs a
   fraction of the modal field's 4000-character limit (~50 chars/item, so
   ~75-80 items of headroom); loot can also be added/replaced later - see
   below. A paste landing exactly at that 4000-char ceiling is rejected
   rather than trusted, since Discord's input box silently truncates
   instead of refusing to submit - use the Add/Update Loot button's file
   upload for an export that large instead (no size limit there).
2. The bot posts a new thread in the raid-summary **forum channel**. The
   top post has: a tier banner image, a TL;DR (bosses down, pulls, loot
   count; first-pull/raid-ended clock times + total duration, anchored to
   the first REAL boss pull rather than whatever time the log recording
   itself started; per-instance clear time - e.g. Black Temple and Mount
   Hyjal tracked separately, each compared to our fastest-ever clear of
   that instance with a ⚡ badge on a new/tied record), a **Links** section
   (the full WCL log + a [Wipefest](https://www.wipefest.gg) analysis
   link), roster composition, a boss-by-boss breakdown (pull count, kill
   time + its difference from our fastest-ever kill of that boss, and every
   *real* wipe indented underneath with the boss's HP% at that wipe - "killed"
   and each "Wipe N" link straight to that specific pull's Wipefest analysis,
   since a WCL fight ID is also Wipefest's own per-fight URL segment. A
   100%-HP "wipe" is treated as a deliberate reset, not a real attempt, and
   doesn't count), three **Raid MVP's** (highest AVERAGE DPS parse across
   all of that raid's kills - matches WCL's own "avg" rankings column,
   highest overall damage done, highest overall healing done - the latter
   two each also show that amount's % share of the raid's total
   damage/healing), a **Noteworthy parses** list (elite 99%+ individual-boss parses, flagged
   as a personal best where applicable), who broke their own personal-best
   parse on a boss tonight, ⚔️ **Top Overall Damage** (bosses + trash, top
   5 with medals 🥇🥈🥉🏅🏅, each person's exact share of the raid's total
   damage), a guild rank vs. other guilds (if `GUILD_NAME` is set), and a
   💀 **Death's Leaderboard** (top 5, same medals). Every name mentioned
   anywhere in the summary - parses, MVP's, leaderboards, loot winners - is
   prefixed with that character's class icon (same icon source as
   `/checkattendance`'s roster; pet/summon entries are filtered out of
   roster composition specifically, since WCL's data can include them).
   It auto-applies forum tags: tier and main/alt-raid by exact tag ID
   (`config.TIER_TAG_IDS` / `RAID_TYPE_TAG_IDS`), clear-status by name
   (`config.CLEAR_STATUS_TAG_NAMES`).
3. **Loot** is always its own separate message (normally the 2nd one) as a
   compact list - one line per item: a real item icon (see below) + a
   clickable Wowhead-linked name + who won it. Never a wall of text mixed
   in with the rest of the summary.
4. Roster composition uses a 70%-threshold role classification, not just
   "whichever role someone appeared under most" - a tank/healer who also
   DPS'd some fights (common on trash, or when short-handed) still counts
   as their main role as long as they filled it in at least 70% of the
   fights they were in that raid; everyone else counts as DPS. This needs
   its own extra WCL fetch (every wipe fight's roster, not just kills - see
   `wcl_client.get_report_role_composition`), so it's the one section of a
   summary that can take a moment longer on a report with a lot of wipes.
5. Fastest-kill, fastest-clear (tracked per real raid instance, not per
   tier - see `config.TIER_SUB_INSTANCES`), and personal-best-parse records
   persist across raids - each new summary compares against whatever's on
   record and only updates it if this run matched or beat it. Clear-time
   tracking is purely data-driven (every boss in that instance killed this
   raid) - independent of the Full Clear/Progress pick, so a "Progress"
   raid can still show a clean per-instance clear if that wing got
   finished. Records are only ever set by posting a new summary, never by
   editing one or adding loot later.
6. Every summary's last message has two persistent buttons (any moderator):
   **✏️ Edit** (update the note/media link via a modal) and **🎁 Add/Update
   Loot** (post the Gargul export - as its own reply in the thread within 5
   minutes of clicking, since Discord modals can't take file uploads - to
   add loot that wasn't ready yet, or replace it if it was wrong). Nothing
   else is editable: the boss/parse/damage/deaths/comp sections are
   computed once at post time and frozen, specifically so an edit can never
   retroactively change an already-shown "first kill"/"fastest"/personal-
   best line (see `cogs/raid_summary.py`'s module docstring for why).
7. The summary is automatically split across as many thread messages as
   needed to stay under Discord's per-message limits - never a single wall
   of text. Editing/adding loot later re-splits and reconciles against
   however many messages already exist (edits in place, adds/removes
   messages if the change altered the page count).
8. All WCL data for a report (fights, parses, damage, deaths) is fetched
   and cached **once per report code**, shared with `/checkattendance` -
   summarizing a log that's already been used for attendance (or vice
   versa) costs zero extra WarcraftLogs requests for anything already
   cached. (Roster composition, per point 4 above, is a separate lazy fetch.)
9. Item names/links come from Wowhead (looked up by ID and cached
   permanently, since Gargul's export only gives an item ID) - see the
   setup step below and `wowhead.py`. Each loot line's icon is a real
   Discord emoji, auto-provisioned the first time that item shows up in a
   summary (reusing `/add-emoji`'s own "Wowhead icon -> bot-owned
   application emoji" mechanism - see `icons.ensure_item_emoji`) - never
   consumes the server's own emoji slots.

## Commands

### Everyone

| Command | Description |
|---|---|
| `/apply <character>` | Apply to the guild with your character name - kicks off the gear-check flow. |
| `/update-application` | Add or update the note/screenshot(s) on your latest pending application. |

### Moderator only

| Command | Description |
|---|---|
| `/announce` | Draft an announcement in the sandbox channel, with Edit/Publish buttons. |
| `/announce-edit-published <message_id>` | Edit a published announcement by message ID - fallback for one with no working Edit button. |
| `/announce-fix-legacy-buttons` | One-time cleanup: strip the old public Edit button from announcements published before it moved to the sandbox. |
| `/add-emoji <wowhead_links>` | Add custom emoji from one or more Wowhead item/spell links. |
| `/gearcheck archive` | Move all approved/denied applications to the archive channel. |
| `/checkattendance run` | Generate/refresh the attendance overview (no cooldown). |
| `/checkattendance addlog <date> <tier> <link>` | Add a main-raid log to the tracked list. |
| `/checkattendance removelog <id>` | Remove a log from the tracked list by its `#ID`. |
| `/checkattendance link <main> <alt>` | Link an alt character to a main, for attendance purposes. |
| `/checkattendance removealt <alt>` | Remove a previously linked alt. |
| `/checkattendance setmain <member> <character>` | Override a member's main character name. |
| `/checkattendance removemain <member>` | Remove a member's main-name override. |
| `/checkattendance links <member>` | Debug: show a member's resolved main name and linked alts. |
| `/checkattendance exclude <name> <reason>` | Excuse a player from attendance tracking. |
| `/checkattendance removeexcluded <id>` | Remove a player from the excused list by its `#ID`. |
| `/raidsummary` (dropdowns for tier/clear status/raid type/report, then a modal for report-link fallback/loot/note/media) | Post a raid summary thread to the raid-summary forum (loot can be pasted directly, added later, or - for an unusually large export - uploaded as a file via the thread's Add/Update Loot button). |
| `/raidsummary-refresh-cache <tier>` | Re-fetch every already-imported report for a tier from WCL, e.g. after changing `config.EXCLUDED_ENCOUNTER_IDS`. Never touches an already-posted summary message. |
| `/raidsummary-refresh-report <report>` | Force-refetch ONE report from WCL before posting/regenerating its summary, without waiting for the next full cache refresh. |
| `/raidsummary-regenerate [report]` | Rebuild an already-posted summary's entire content from fresh WCL/Wowhead data, in place. |
| `/raidsummary-merge-weeks <tier> <reports>` | Fold two or more already-imported reports into one raid week for `/tier-recap`'s stats, optionally consolidating their separate summary posts into one thread. |
| `/tier-recap <tier>` | Draft an end-of-tier stats recap (medals, records, most-improved, etc.) in the sandbox channel, same draft/Edit/Publish flow as `/announce` - built entirely from data already cached by past `/raidsummary` posts, no new WCL calls. |

Five more commands exist purely for diagnosing API/lookup issues or doing a
one-time data migration/import, and are disabled by default
(`config.DEBUG_COMMANDS_ENABLED = False`) so they don't clutter the command
list day-to-day. Set that flag to `True` to use one, then set it back off:

| Command | Description |
|---|---|
| `/apply-test-blizzard <character>` | Step-by-step test of a character's Blizzard Armory data (gear/talents) - dumps raw responses to spot a namespace/field mismatch. |
| `/raidsummary-test-blizzard [item]` | Same, for a single item against Blizzard's Game Data API. |
| `/raidsummary-test-spell [spell]` | Same, for a single spell's icon. |
| `/raidsummary-refresh-wowhead` | One-time cache wipe for Wowhead item/spell lookups (was for the retail→`/tbc/` endpoint migration; safe to run anytime, just costs a fresh fetch of everything). |
| `/raidsummary-bulk <tier> <raid_type> <reports>` | One-time bulk import: posts one raid summary per WCL report, oldest first - for backfilling history when first setting up. |

## Enabling/disabling features

Every feature past guild applications (`/apply`, always on) is its own cog
and can be turned off independently via a `FEATURE_*_ENABLED` flag near the
top of `config.py` - no code changes, no editing `bot.py`'s extension list,
just flip the flag and restart:

| Flag | Cog | Disables |
|---|---|---|
| `FEATURE_ANNOUNCEMENTS_ENABLED` | `cogs/announcements.py` | `/announce` and its edit/publish flow |
| `FEATURE_EMOJI_ADMIN_ENABLED` | `cogs/emoji_admin.py` | `/add-emoji` |
| `FEATURE_ATTENDANCE_ENABLED` | `cogs/attendance.py` | `/checkattendance` and all its subcommands |
| `FEATURE_RAID_SUMMARY_ENABLED` | `cogs/raid_summary.py` | `/raidsummary` and every `raidsummary-*` command |
| `FEATURE_RAID_LOGS_ENABLED` | `cogs/raid_logs.py` | the #logs tagging/auto-Summarize automation (section 11 below) |
| `FEATURE_TIER_RETROSPECTIVE_ENABLED` | `cogs/tier_retrospective.py` | `/tier-recap` |

Two of these lean on another cog to actually do anything:
`FEATURE_RAID_LOGS_ENABLED` needs both `FEATURE_RAID_SUMMARY_ENABLED` and
`FEATURE_ATTENDANCE_ENABLED` on, and `FEATURE_TIER_RETROSPECTIVE_ENABLED`
needs `FEATURE_RAID_SUMMARY_ENABLED` on (it reads that cog's cached
report data). Neither combination crashes if you get it wrong - the
dependent cog still loads, its commands just no-op with an explanatory
message - but `bot.py` logs a warning at startup if it spots the
mismatch, so check the console after changing these.

## Setting up on a new server

### 1. Create the Discord application and bot

1. https://discord.com/developers/applications -> New Application -> Bot tab.
2. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent** (needed to assign roles/fetch members)
   - **Message Content Intent** (needed so `message.attachments` is
     populated for the note/screenshot DM step - without it, every message
     looks like it has zero attachments to the bot)
3. Copy the bot token for later (`.env`'s `DISCORD_TOKEN`).
4. OAuth2 -> URL Generator: scopes `bot` + `applications.commands`;
   permissions `View Channels`, `Send Messages`, `Embed Links`,
   `Add Reactions`, `Read Message History`, `Manage Roles`,
   `Manage Nicknames`, `Manage Messages`. Open the generated URL to invite
   the bot.
5. In Server Settings -> Roles, make sure the bot's own role sits **above**
   the "Fresh" role - Discord's hierarchy rule applies to both role
   assignment and nickname changes.

### 2. Create the channels and roles you'll need

- A **landing channel** (e.g. `#gear-check`) - visible to everyone,
  including new/unverified members.
- A **review channel** - mod-only, where application cards get posted for
  ✅/❌ decisions.
- An **announcement sandbox channel** - mod-only, where announcement drafts
  get tweaked before publishing.
- A **"Fresh" role** - granted automatically on approval.
- A **moderator role** (or just rely on anyone with `Manage Roles` - both
  work; the mod role is for people who should be able to approve/deny
  without needing full role-management permissions generally).

### 3. Lock the landing channel down (optional but recommended)

If you want new members to see *only* the landing channel until approved:
in `@everyone`'s permissions, deny `View Channel` on every other
channel/category, then grant it back specifically to the "Fresh" role on
each one (an explicit role-level Allow overrides an `@everyone` Deny on the
same channel). Keep the review and sandbox channels visible only to your
moderator role.

### 4. Get WarcraftLogs API credentials

1. https://www.warcraftlogs.com/api/clients/ -> Create Client.
2. Redirect URI: anything (e.g. `http://localhost`) - unused, since the bot
   only uses the `client_credentials` flow.
3. Leave **"Public Client"** unchecked - the bot can store the secret
   securely and needs the full client_credentials flow.
4. Copy the Client ID and Secret for `.env`.

### 5. Find your realm's server slug

WarcraftLogs' internal slug isn't always identical to the in-game realm
name. Search a guildmate's character on fresh.warcraftlogs.com and read it
from the URL:

```
https://fresh.warcraftlogs.com/character/<region>/<THIS PART>/CharacterName
```

### 6. Fill in `.env`

Copy `.env.example` to `.env` and fill in every value:

| Variable | What it's for |
|---|---|
| `DISCORD_TOKEN` | From step 1 |
| `WCL_CLIENT_ID` / `WCL_CLIENT_SECRET` | From step 4 |
| `REVIEW_CHANNEL_ID` | The mod-only review channel from step 2 |
| `GEAR_CHECK_CHANNEL_ID` | The landing channel - the bot auto-posts and pins a "how to apply" button here on startup. Optional; leave blank to skip and post your own |
| `ANNOUNCEMENT_SANDBOX_CHANNEL_ID` | The sandbox channel from step 2 - required for `/announce` |
| `FRESH_ROLE_ID` | The role granted on approval |
| `MOD_ROLE_ID` | Who can approve/deny/reset/edit announcements (in addition to anyone with `Manage Roles`) |
| `SERVER_SLUG` / `SERVER_REGION` | From step 5 |
| `ATTENDANCE_CHANNEL_ID` | The channel for the three pinned attendance-check messages - required for `/checkattendance` (see `cogs/attendance.py`) |
| `MODERATOR_CHANNEL_ID` | Optional - your general moderator chat. Every Attendance Overview refresh that has a real acting user/label also mirrors the overview here, on top of the pinned post in `ATTENDANCE_CHANNEL_ID`. Leave blank to skip |

Never commit the real `.env` anywhere - only `.env.example` (no secrets) is
meant to be shared/versioned.

### 7. Confirm the raid tier's zone/encounter IDs

`config.py`'s `CURRENT_TIER`/`PREVIOUS_TIER` need real WarcraftLogs zone and
encounter IDs, which aren't always the same as what `worldData.zones`
reports - the ones actually accepted by `character.zoneRankings` can differ.
Set `config.DEBUG_COMMANDS_ENABLED = True` temporarily and run
`/raidsummary-refresh-report` against a real report from that tier - its
diagnostic breakdown confirms whether the zone/encounter IDs currently in
`config.py` actually match, before trusting the tier-performance section.
Turn `DEBUG_COMMANDS_ENABLED` back off once confirmed.

### 8. Class/role/spec icons (optional, but nicer)

`config.py`'s `CLASS_ICON_URLS`, `ROLE_ICON_URLS`, and `SPEC_ICON_URLS` take
a stable image URL per icon (a Wowhead-hosted icon works well) - the bot
downloads and uploads each one itself as a bot-owned "application emoji" on
startup, no manual server upload needed. `CLASS_EMOJI_NAMES`/
`ROLE_EMOJI_NAMES` are a fallback if you'd rather manually upload guild
emoji for some of them instead (or already have). Everything falls back to
plain text if an icon isn't configured or fails to load - nothing crashes
either way.

### 9. OXM.gg raid sign-up integration (optional)

If you use OXM.gg for raid sign-ups, set `config.OXM_REGISTER_COMMAND_ID`
to their `/register` command's real numeric ID (enable Developer Mode,
type `/register` where OXM's bot is present, and copy the command ID from
the client). The approval DM then includes a clickable mention of that
command. This only renders as truly clickable-to-prefill in a context
where OXM is actually present (e.g. a real server channel) - in a DM
between the applicant and this bot, it typically falls back to plain text,
which is why the DM also includes a plain-text fallback instruction.

### 10. Raid summary forum (optional, for `/raidsummary`)

1. Create a **Forum channel** (not a text channel) for raid recaps.
2. In that channel's settings, add: two tags for the tiers (e.g.
   `BT/Hyjal`, `SSC/TK`), two for main vs. alt/fun raids (e.g. `Mainraid`,
   `Altraid`), and two for clear status (`Full Clear`, `Progress`). The
   bot only applies tags that already exist here - it never creates or
   edits forum tags itself. Tier and main/alt tags are matched by exact
   Discord tag ID, not name - after creating them, enable Developer Mode,
   right-click each tag, Copy ID, and put the four IDs into
   `config.TIER_TAG_IDS` / `config.RAID_TYPE_TAG_IDS`. Clear-status tags
   are matched by name instead (`config.CLEAR_STATUS_TAG_NAMES`), so those
   two just need to be named exactly `Full Clear`/`Progress`.
3. Set `RAID_SUMMARY_FORUM_CHANNEL_ID` in `.env` to that channel's ID.
4. Optionally set `GUILD_NAME` in `.env` (exact WarcraftLogs guild name) to
   enable the "guild rank" section.
5. Optionally set a banner per tier in `config.RAID_TIER_BANNERS` - either a
   plain image URL, or (the default) a local file path under `images/`
   (e.g. `images/banner-bt.jpg`). Local files just need to exist on disk
   next to the bot; nothing to upload anywhere manually. A tier with a
   missing/unconfigured banner just posts without one.
6. Optionally set `RAID_LOGS_CHANNEL_ID` in `.env` to the channel your
   WCL-report-posting webhook/app announces new reports in (e.g.
   "Crusader's Logs"). When set, `/raidsummary`'s dropdown step offers the
   most recent reports posted there, labeled by their report title, so a
   moderator can pick one by name instead of pasting a link - see
   `_extract_log_report_code` in `cogs/raid_summary.py` for how it finds
   the report code inside that channel's embeds (checks the description,
   every field, the embed's own URL, and the title, so it isn't tied to
   one exact field layout). Leave it unset to skip straight to pasting a
   link in the modal.
7. **Before relying on it**, post one real summary and sanity-check the
   loot section: item names/icons come from Wowhead's `&xml` data feed (see
   `wowhead.py`) - if they keep coming back as "Item #NNNNN" placeholders,
   test a single known item ID first.

### 11. Raid log tagging & attendance automation (optional, for `cogs/raid_logs.py`)

Turns the post-raid #attendance-check checklist (attach the log, refresh
roster, refresh overview, notify the mods, run `/raidsummary`) into
tag-the-log-once-and-click-Summarize. Needs `RAID_LOGS_CHANNEL_ID` (step 10
above) AND a second channel:

1. **Hide `RAID_LOGS_CHANNEL_ID`'s channel from members** - deny `View
   Channel` for `@everyone` there, but make sure the bot's own role can
   still see it (and that your WCL-report-posting webhook/app can still
   post to it). This bot never attaches buttons to that channel's own
   messages (it doesn't own them - a third-party app posts there); it only
   reads them.
2. Create a second, **visible** channel - this is where the bot reposts a
   cleaned-up version of each new log (reporter, when it started, the
   zone, a link - deliberately not the Wipefest tool link or `/listen`
   instructions some third-party posts include) with its own tagging
   buttons attached.
3. Set `RAID_LOGS_REPOST_CHANNEL_ID` in `.env` to that second channel's ID.
   Leave it blank to disable this whole feature - `RAID_LOGS_CHANNEL_ID`
   and `/raidsummary`'s picker keep working standalone either way.
4. Make sure `config.ORGANIZER_ROLE_ID` matches your real Organizer role -
   tagging a log (Main Raid/Alt Raid/Other) and Reset are Organizer-only;
   Summarize (manual or the daily auto-complete) stays moderator-only, same
   gate as everything else in this bot.
5. `config.RAID_LOG_AUTO_SUMMARIZE_TIME` (default `"23:59"`, Europe/
   Amsterdam) is the daily cutoff past which a tagged-but-not-yet-
   Summarized log auto-runs the automatable part of Summarize (attendance
   attach/refresh/notify for a Main Raid tag - the Gargul loot paste always
   still needs a moderator, so "Post Raid Summary" is left as a button
   either way). `config.RAID_LOG_DUPLICATE_WINDOW_MINUTES` (default `20`)
   controls how close together two "started a new report" posts for the
   same zone have to be before the second one is folded into the first
   instead of getting its own repost - see `cogs/raid_logs.py`'s module
   docstring for why this exists (multiple people starting a live log for
   the same raid at once) and how it degrades (never silently discarded -
   folded into the existing message as "also started by X"). Set
   `MODERATOR_CHANNEL_ID` (step 6 above) if you also want every attendance
   overview refresh - automatic or manual - mirrored to your mod chat.
6. **Before relying on it**, watch the first real log come through: the
   #logs embed parsing (`_parse_source_embed` in `cogs/raid_logs.py`) was
   written against one real example post, not verified against every
   layout your specific webhook/app might use - see that function's
   docstring. A parsing miss just means a blank reporter/description, never
   a crash - but it's worth a glance the first time.

### 12. Install and run

```bash
pip install -r requirements.txt
python bot.py
```

Check the console for `Synced N command(s): [...]` and
`Logged in as ... (id=...)` to confirm startup succeeded.

## Architecture

```
bot.py              - entry point: sets up the bot, shared singletons, loads cogs
config.py           - all tunable config (colors, tiers, specs, icon sources, OXM command ID)
wcl_client.py        - WarcraftLogs API client (fights/rankings/deaths/roster, cached per-report)
wowhead.py           - Wowhead item name/icon/link lookup (cached permanently by item ID)
blizzard_client.py   - optional Blizzard Game Data API client (item/spell icons, character
                        armory) - preferred over wowhead.py when BLIZZARD_CLIENT_ID/SECRET
                        are set, otherwise unused
gargul_loot.py       - parser for Gargul's loot-export CSV format
storage.py           - tiny JSON-backed persistence (generic - any cog can use it)
icons.py             - shared icon resolution (guild emoji + auto-provisioned application emoji)
cogs/
  apply.py           - the whole guild-application feature (self-contained)
  announcements.py   - the whole announcement feature (self-contained)
  emoji_admin.py     - /add-emoji (Wowhead link -> bot-owned application emoji)
  attendance.py      - the whole attendance-tracking feature (self-contained)
  raid_summary.py    - the whole raid-summary feature (self-contained)
  tier_retrospective.py - /tier-recap, an end-of-tier stats recap built entirely from
                        data raid_summary.py already cached (see its own module docstring)
  raid_logs.py       - raid log tagging + attendance/raidsummary automation
                        (self-contained; reuses raid_summary.py's
                        RaidSummaryOptionsView and attendance.py's
                        addlog/refresh methods directly, cross-cog)
```

**Adding a new feature later** (e.g. attendance tracking): create a new
file in `cogs/`, add one line to `EXTENSIONS` in `bot.py`. You shouldn't
need to touch `cogs/apply.py` or `cogs/announcements.py` at all unless the
new feature actually needs to change how those work.

## How `bot.py` operates

`bot.py` is deliberately thin - it doesn't contain any feature logic itself:

1. Builds the bot with the intents every cog needs (members, reactions,
   message content).
2. Creates two **shared singletons** every cog can reach via
   `self.bot.<name>` - `bot.wcl` (the WarcraftLogs client) and `bot.store`
   (the JSON persistence store) - so no cog has to instantiate its own copy.
3. On `on_ready`: provisions any configured application emoji (`icons.py`),
   syncs the slash-command tree, and logs both so startup problems are
   visible immediately.
4. Loads every file listed in `EXTENSIONS` via `bot.load_extension(...)`
   before starting the bot - this is what actually registers each cog's
   commands and listeners. If a new cog's commands aren't showing up in
   Discord, checking whether it's in this list (and whether it loaded
   without error) is the first thing to check.

Each cog manages its own persistent UI components (buttons that need to
keep working across bot restarts) by registering one template instance via
`bot.add_view(...)` inside its own `cog_load()` - see the Reset button in
`apply.py` or the Edit button in `announcements.py` for the pattern to copy
when adding a new persistent component elsewhere.

## Notes / known limitations

- **Gear display was removed.** The WCL query needed for reliable
  equipped-gear data didn't pan out reliably; the optional screenshot step
  covers this need instead.
- **Components V2 requires discord.py 2.6+** (pinned in `requirements.txt`).
  The review cards and announcements both use Discord's newer
  Container/TextDisplay/Separator/MediaGallery component system instead of
  classic embeds, for real divider lines and cleaner layout control.
- **Persistent-view registration for `LayoutView`** (used for the
  Reset/Edit buttons) is assumed to work the same way it does for classic
  `View` - this wasn't independently verified beyond normal testing, so if
  a persistent button stops responding specifically after a bot restart,
  that's the first thing to suspect.
- **Persistence**: `applications.json` and `announcements.json` hold all
  pending/posted state so a restart doesn't lose anything. They're plain
  JSON files in the working directory - fine at this scale, but not
  written for high-concurrency or multi-process use.
- **`/raidsummary`'s WCL parsing needs a live verification pass.** It was
  written without WCL credentials available, so some pieces are best-effort
  rather than confirmed against a real response: the `Report.rankings`
  JSON shape in `wcl_client.py` (parsed defensively - a shape mismatch just
  empties that section instead of crashing; the boss-name field in
  particular was already wrong once - see `_parse_rankings`'s docstring -
  and now has two independent fallback paths, but is still worth watching),
  the Deaths/DamageDone/Healing table shapes, and the shape (not the field
  name - `Guild.zoneRanking`, singular, was confirmed live via a WCL error
  message after the original `zoneRankings` guess failed) returned by the
  guild-rank query. `playerDetails` per fight has also been seen live
  returning `[]` instead of the expected role-bucketed dict for some
  fights - handled, but a reminder these WCL shapes vary more than the
  docs suggest. Post one real summary and check each section actually
  populated before trusting it. Item lookups (`wowhead.py`)
  reuse the same `&xml` endpoint `/add-emoji` already relies on, so that
  part carries the same (lighter) caveat as that existing command. The
  ✏️ Edit button's `message.edit(view=...)` call on an already-posted forum
  thread message follows the same pattern already used for announcements'
  Edit button, but specifically for a `LayoutView`-based forum *thread*
  message (as opposed to a plain channel message) wasn't independently
  re-verified here either. The 🎁 Add Loot button's `bot.wait_for("message", ...)`
  flow is a standard discord.py pattern but is new to this codebase - worth
  a real test (does the timeout message show up correctly, does the
  uploaded message get cleaned up) before relying on it live. Roster
  composition's 70% threshold (`wcl_client.get_report_role_composition`)
  reuses the already-verified `playerDetails` shape, just against wipe
  fights instead of kills - lower risk than the other best-effort pieces
  above, but still worth a glance the first time a report with real wipes
  goes through it.
- **`cogs/raid_logs.py`'s #logs embed parsing needs the same kind of live
  check.** `_parse_source_embed` was written against one real example post,
  matching the exact "X started a new report" / zone line / Tools+Wipefest
  field / `/listen`-block layout seen at the time - not against every shape
  that webhook/app might use. A parsing miss degrades gracefully (a blank
  reporter/description, never a crash) as long as the WCL report link
  itself is still found somewhere in the embed - but watch the first few
  real reposts to confirm reporter/description come through correctly.
  Duplicate-live-log folding (`_find_duplicate_entry`) is a same-zone-text
  + time-window heuristic, not a guarantee - it only ever folds a match
  into the existing repost (never silently drops one), so a false negative
  just means two reposts for the same raid instead of one, easy to spot
  and clean up manually. Only `config.CURRENT_TIER`/`PREVIOUS_TIER` are
  ever offered/guessed as a tier for the Summarize → Post Raid Summary
  hand-off - older content (e.g. Karazhan, Gruul's Lair/Magtheridon's Lair)
  isn't wired in, since that needs real WCL zone/encounter IDs confirmed
  live against a real account (see step 7 of setup above), not guessed.
