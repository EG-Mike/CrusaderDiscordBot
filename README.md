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
   gets marked "Published to #channel by [mod]" and loses its buttons.
4. Every published announcement keeps its own persistent Edit button - any
   moderator, not just whoever posted it, can tweak it afterward.
5. Formatting: `## Header text 🎉` and any emoji work directly (standard
   Discord message markdown - no special handling needed). A line
   containing just `---` splits the message into separate sections, each
   with a real divider between them.

### Raid summaries

1. A moderator runs **`/raidsummary`** with the short fields (tier, WCL
   report link, full-clear-or-progress, main-or-alt raid), which opens a
   **modal** for the free-text ones: the Gargul loot export **pasted
   directly**, a note, and a YouTube/Twitch/image link. The loot paste has
   to go through a modal rather than a plain slash-command option - a
   slash-command string option is a single-line input in Discord's client,
   so pasting a multi-line export into one silently collapses every
   newline to a space and the export becomes unparseable. A modal's
   paragraph text field is the only Discord input that keeps real newlines
   intact. A normal night's export runs a fraction of the modal field's
   4000-character limit (~50 chars/item, so ~75-80 items of headroom); loot
   can also be added/replaced later - see below. A paste landing exactly at
   that 4000-char ceiling is rejected rather than trusted, since Discord's
   input box silently truncates instead of refusing to submit - use the
   Add/Update Loot button's file upload for an export that large instead
   (no size limit there).
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
   highest overall damage done, highest overall healing done), a
   **Noteworthy parses** list (elite 99%+ individual-boss parses, flagged
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
| `/raidsummary <tier> <report> <clear_status> <raid_type>` (opens a modal for loot/note/media) | Post a raid summary thread to the raid-summary forum (loot can be pasted directly, added later, or - for an unusually large export - uploaded as a file via the thread's Add/Update Loot button). |

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

Never commit the real `.env` anywhere - only `.env.example` (no secrets) is
meant to be shared/versioned.

### 7. Confirm the raid tier's zone/encounter IDs

`config.py`'s `CURRENT_TIER`/`PREVIOUS_TIER` need real WarcraftLogs zone and
encounter IDs, which aren't always the same as what `worldData.zones`
reports - the ones actually accepted by `character.zoneRankings` can differ.
Run `debug_rankings.py "SomeCharacterName"` against a character with real
logs in that tier and confirm the zone ID matches what's currently in
`config.py` before trusting the tier-performance section.

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
6. **Before relying on it**, post one real summary and sanity-check the
   loot section: item names/icons come from Wowhead's `&xml` data feed (see
   `wowhead.py`) - if they keep coming back as "Item #NNNNN" placeholders,
   test a single known item ID first.

### 11. Install and run

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
gargul_loot.py       - parser for Gargul's loot-export CSV format
storage.py           - tiny JSON-backed persistence (generic - any cog can use it)
icons.py             - shared icon resolution (guild emoji + auto-provisioned application emoji)
cogs/
  apply.py           - the whole guild-application feature (self-contained)
  announcements.py   - the whole announcement feature (self-contained)
  attendance.py      - the whole attendance-tracking feature (self-contained)
  raid_summary.py    - the whole raid-summary feature (self-contained)
debug_zones.py        - lists every WCL zone/encounter ID visible via the API
debug_rankings.py     - dumps raw zoneRankings JSON for one character - the
                        source of truth for a zone ID, since debug_zones.py's
                        catalog IDs don't always match what zoneRankings itself accepts
debug_lookup.py       - simple character lookup sanity check
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
