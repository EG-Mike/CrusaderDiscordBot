# CLAUDE.md: project brief for Crusader Discord Bot

Read this before changing anything. It describes where the project is going,
not just where it is.

## Direction

The bot started as a single-purpose WoW guild application bot and grew by
bolting features on. It is now being turned into a **scalable, formalised,
manageable** bot. Priorities, in order of importance when they conflict:

1. **Security**: least privilege, secrets never logged or sent to a browser,
   server-side permission checks, validated input.
2. **Usability**: for moderators in Discord and in the web dashboard. Clear
   wording, sensible defaults, no raw IDs where a picker can be used, and
   errors that say what to do.
3. **Configurability**: everything a guild might want to tune is a setting
   editable in the dashboard, not a code change or an `.env` edit.
4. **Professionalism and maintainability**: consistent structure, typed and
   documented code, tests, no dead code, no one-off hacks.
5. **Performance**: cache external API data, avoid redundant calls, keep
   Discord interactions within their response deadlines.

Features should be **as modular as makes sense without hurting usability**:
one feature = one self-contained module that can be switched off, with its
own settings and (when it has UI) its own dashboard page.

Rewriting existing code is welcome when it serves these goals better than
patching it. Explain the trade-off when doing so.

## Game context: moving from TBC to "WoW Forever"

The bot was written for WoW TBC Anniversary (Classic "Fresh" realms). The
guild is moving to **WoW Forever**, a new version of the game on a
**modern client**. Expect:

- Item IDs, spell IDs, zone/encounter IDs, Wowhead URLs and WarcraftLogs
  hosts/URLs to **change**. Nothing game-version-specific may be hardcoded:
  it belongs in data/config (per game version), with the current values as
  data, not code.
- **Blizzard's API to be the primary data source** (characters, gear,
  guild roster, item/spell media, etc.), since a modern client exposes far
  more than Classic did. Wowhead scraping is a fallback at most.
- TBC-specific logic (talent trees, `config/game_data.py`, tier
  definitions, the TBC raid tiers) to be replaced or made pluggable, not
  extended.

When touching game-data code, prefer designs where supporting a new game
version or raid tier is a configuration change.

## Environments

- **Live bot**: runs from `main` on a VPS under **AMP**, configured with
  the original `.env` file. It must keep working. Anything merged to
  `main` must be backwards compatible with an existing `.env` and JSON data
  files, or come with explicit migration steps.
- **Test bot**: a separate Discord application in a private test server,
  running as a Docker container on the same VPS (`/opt/crusader-test`,
  see `deploy/test/`). GitHub Actions builds the image on every push
  (`.github/workflows/docker-image.yml`; tag `test` for the `beta` branch, `latest` for `main`, for **amd64 and arm64**; the VPS is ARM).
  The VPS pulls new builds every 5 minutes. **The test environment can be
  changed freely**: breaking changes and rewrites are fine there.
- **Branches**: work happens on `beta` (the test bot runs it); merging
  `beta` into `main` releases it to the live bot. Push to `beta`, not to a
  new branch of your own.
- The dashboard is reached over an SSH tunnel (PuTTY) at
  http://localhost:8081. It is not exposed to the internet.
- **Dev bot (cloud sessions)**: Claude Code cloud sessions for this repo
  have credentials for a third Discord application, "Crusader Dev", which
  is in the same test server, plus WarcraftLogs and Blizzard API keys, as
  environment variables (`DISCORD_TOKEN`, `WCL_CLIENT_ID`,
  `WCL_CLIENT_SECRET`, `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`), and
  outbound access through the session's proxy. Verify with
  `python scripts/check_integrations.py` (never prints secrets): it checks
  the credentials and every host the bot uses after logging in. The
  environment's network access must allow (Custom, defaults included):
  `*.discord.gg`, `discord.com`, `*.discordapp.com`, `*.discordapp.net`,
  `*.warcraftlogs.com`, `*.battle.net`, `*.api.blizzard.com`,
  `render.worldofwarcraft.com`, `*.wowhead.com`, `*.zamimg.com`. To run
  the bot itself, set `DISCORD_PROXY=$HTTPS_PROXY` (aiohttp ignores
  HTTPS_PROXY otherwise). Tests: `python -m unittest discover -s tests -t .`.
  Use
  it to run the bot for real and test against the live APIs. Keep
  WarcraftLogs usage modest: the client is shared with the test bot, so
  they share one hourly rate limit. Never print, log or commit these
  values.

## Architecture (current)

- `bot.py`: entry point, shared clients (`bot.wcl`, `bot.wowhead`,
  `bot.blizzard`, `bot.store`), loads feature cogs by `FEATURE_*` flag.
- `cogs/`: one cog per feature. `web_dashboard.py` starts the dashboard.
- `config/`: `deployment.py` holds the defaults for every setting;
  `overrides.py` layers `deployment_local.py` (gitignored) and then
  dashboard-saved values on top, at startup.
- `storage.py`: key/record stores, JSON files or SQLite
  (`STORAGE_BACKEND`); `db.py`: the shared SQLite database; `db_tools.py`:
  info/export/backup, and `import-old` to merge JSON files from an older
  copy into the database (never overwrites newer records).
- `audit.py`: the audit trail (structured events in the database,
  mirrored to the `audit.log` text file); written by `cogs/audit_log.py`
  (Discord) and `webapi.server.audit` (dashboard), read by the dashboard.
- `system_log.py`: bot lifecycle events (start, stop with reason,
  update, unexpected stop, Discord outages) and console warnings/errors
  captured from logging, secrets redacted; `app_version.py`: which build
  is running. Console output is admin-only in the dashboard.
- `announcements.py`: announcements (drafts, publishing, edits, versions,
  templates), shared by `cogs/announcements.py` and the dashboard.
- `bot_messages.py`: the bot's standard texts (pinned explainers, DMs)
  with their defaults and `{placeholders}`; cogs call `bot_messages.embed()`
  / `.text()` instead of hardcoding text.
- `emoji_library.py`: the bot's WoW icon emoji, one per icon image (keyed
  by Blizzard's icon file ID / icon name) with the items and spells that
  share it; Blizzard API first, Wowhead tooltip only as a fallback. Used by
  the Emoji page, the editor emoji picker and raid summaries.
- `applications.py`: application records shared by `cogs/apply.py` and
  `webapi/applications_api.py` (the Applications page): finding records
  (also by their pre-archive ID), how a record looks to the dashboard, and
  change notifications (`on_change`/`changed`, broadcast to the dashboard
  as `applications.changed`). The decisions themselves (`approve`, `deny`,
  `reset`, `refresh_gear`, `archive_decided`) are interaction-free methods
  on the apply cog, called by the card's buttons and the dashboard alike;
  moderators and approvers (`APPROVER_ROLE_ID`, dashboard level
  "approver") decide (`ApplyCog.can_decide`, `DECIDE_LEVELS`).
  The applicant enters "First Surname" in one field; the main spec is
  suggested from talent points, plus an optional offspec. Item names link
  to `ITEM_LINK_URL` (Wowhead's Forever pages by default). Approval links
  the character on the roster, or leaves a pending link
  (`roster.remember_link`) when Blizzard doesn't know it yet.
- `gear.py`: the gear check: a character's gear (item level, enchants,
  gems, sets), talents and profile from Blizzard, flags (empty slots,
  missing enchants, empty sockets) and the guild's minimum level/item
  level, as plain data plus Discord text. Used by `cogs/apply.py` (the
  review card, "Refresh from Blizzard", `/lookup`) and the dashboard's
  Character lookup page (`webapi/lookup_api.py`), which also shows
  professions and the character's place on the roster. Character
  statistics (`parse_stats`; `stat_highlights` per role on the card,
  `stat_groups` on the dashboard). Characters on a
  ruleset that isn't the guild's (`CHARACTER_RULESETS`) are flagged. Item
  icons: `wow_api.icon_urls` lists fallback addresses, since Blizzard's
  image server lacks some icons under the Forever path. WarcraftLogs
  parses on applications are optional (`APPLICATION_WARCRAFTLOGS_ENABLED`)
  until WarcraftLogs covers Forever.
- `roster.py`: the guild roster: in-game characters (from the guild's
  Blizzard roster and character profiles), their levels, links to Discord
  members (one main each), specs, and level-up events. Shared by
  `cogs/roster.py` (sync + `/character`), `cogs/leveling.py` (profile
  checks every few minutes, milestone congratulations via the
  "level_milestone" bot message, `/leaderboard`) and
  `webapi/roster_api.py` (Roster and Leveling pages). The roster is also
  kept posted in a Discord channel (`GUILD_ROSTER_CHANNEL_ID`; edited only
  when its content changes; header is the "guild_roster_header" bot
  message; aligned columns with a header row; alts below
  `GUILD_ROSTER_POST_MIN_LEVEL` left out; persistent "Claim a character" /
  "My characters" buttons for main/alt self-service). Level milestones
  found together are grouped into as few messages as fit. Primary
  professions (`roster.professions`) are read after a logout and shown on
  the posted roster with profession emoji (`emoji_library` kind
  "profession"; `GUILD_ROSTER_POST_PROFESSIONS`). Columns added to existing
  tables go in `db._ADDED_COLUMNS`. `permissions.py`:
  the shared Discord moderator check (`is_moderator(member)`,
  `member_is_moderator(guild, user_id)`); every cog uses it.
- `wow_api.py`: Blizzard game data for the configured game version
  (WoW Forever by default): reference data for pickers, a player's
  characters from their Battle.net account, character lookups by name, and
  the admin API explorer (Maintenance page). Namespaces, locale and lookup
  paths are settings. What Forever's API offers is in
  `docs/wow-forever-api.md`; re-check it with
  `scripts/probe_blizzard_api.py`, which writes a shareable report.
- `cogs/notifications.py`: alerts in a moderator channel, built from the
  System log and the audit trail. Settings named `NOTIFY_*` /
  `NOTIFICATIONS_*` apply immediately (`webapi/settings.LIVE_PREFIXES`):
  use that pattern only for settings read at the moment they're used.
- `secret_store.py`: external API keys, Fernet-encrypted in the database
  (key file `secret.key` next to it). Write-only towards the browser.
- `webapi/`: aiohttp dashboard API inside the bot process: Discord OAuth
  login, DB-backed sessions, role-based access (moderator/approver/admin),
  CSRF header check, settings, integrations, setup wizard, restart.
  `server.py` wires the app; feature APIs live in their own module
  (`announcements_api.py`, `polls_api.py`, `maintenance_api.py`, ...)
  using `common.py`. When a cog feature gets a dashboard page, move its
  rules into interaction-free methods both sides call (see `cogs/polls.py`).
- `web/`: dashboard frontend (React + TypeScript + Vite + Tailwind). The
  dark theme is inspired by (not copied from) oxm.gg, with a "crusader
  gold" accent.

### Where configuration belongs

| Where | What |
|---|---|
| `.env` | Only what's needed to start and log in: `DISCORD_TOKEN`, `DISCORD_CLIENT_SECRET`, `WEB_BASE_URL`, `WEB_ADMIN_USER_IDS`, plus hosting options. |
| Dashboard settings (DB) | Channels, roles, realm, feature toggles, tuning values. Defaults in `config/deployment.py`. |
| Dashboard integrations (DB, encrypted) | External API keys. |

Never add a new required `.env` variable for something a moderator could
set in the dashboard.

## Conventions

- **A new feature** is a new cog file plus a `FEATURE_*_ENABLED` flag; its
  settings go in `config/deployment.py` (defaults, with comments for
  developers) and in `webapi/settings_catalog.py` (the dashboard's tab,
  label, help text and editor, written for moderators: plain words, no
  setting names; raw data and fine-tuning marked advanced). A test fails
  until every setting is in the catalog or hidden with a reason.
  Missing configuration must make the feature inactive with a clear
  message, never crash the bot.
- **Outbound HTTP**: create sessions with `aiohttp.ClientSession(trust_env=True)`
  so a host's proxy settings are honoured (the dev environment needs it;
  without a proxy nothing changes). Prefer Blizzard's API over Wowhead
  (Wowhead blocks datacenter IPs).
- **Texts the bot posts on its own** belong in `bot_messages.py` (editable
  on the dashboard), not in cog code.
- **Discord IDs** are snowflakes larger than JavaScript's safe integer
  range: always send them to the browser as strings (`webapi/settings.js_safe`).
- **Errors**: log them with the standard `logging` module (`log.exception`
  for tracebacks); they reach the dashboard's System log automatically.
  Never put secrets in log messages (they're redacted, but don't rely on it).
- **Audit**: user actions are logged (Discord interactions by
  `cogs/audit_log.py`, dashboard actions via `webapi.server.audit`). Never
  log secrets or private text such as applicant notes.
- **Permissions**: check them server-side on every request or interaction;
  the UI only hides what the server already refuses.
- Keep Python compatible with 3.9+ syntax (the live AMP host's Python
  version is unknown); the Docker image uses 3.12.
- Before pushing: byte-compile the Python, run the dashboard build
  (`cd web && npm run build`), and run or extend the tests covering what
  changed. Test UI changes with screenshots at desktop and phone widths.
- Don't put model names or AI tool identifiers in code, comments or docs.

## Roadmap (web dashboard)

- Phase 0 (done): foundation, login, settings, SQLite, Docker, setup
  wizard, encrypted integrations, restart.
- Phase 1 (done): Audit log page (structured storage in `audit.py`,
  filters, live feed, CSV export), and the Overview activity feed.
- Phase 2 (done): Announcement builder (`announcements.py`: live
  Discord-style preview, autosaved drafts shared with `/announce`,
  publish/edit in place, version history, templates). No scheduled
  posting. Plus the Bot messages page (`bot_messages.py`).
- Emoji (brought forward, done): Emoji page with item/spell search and
  icon reuse, and an emoji picker in the editors.
- Phase 3 (done): Polls page (`webapi/polls_api.py` on the polls cog's
  shared rules), command palette (Ctrl+K), notifications to a moderator
  channel (`cogs/notifications.py`), and maintenance tools
  (`webapi/maintenance_api.py`: backup, retention, compact, caches,
  command re-sync, sessions).
- Guild roster and Leveling (done, for WoW Forever): roster synced from
  Blizzard, `/character` linking, milestone congratulations, leaderboard,
  Roster and Leveling pages. Achievements can reuse `roster_events` once
  Blizzard reliably reports them.
- Phase 4 (in progress): applications moved to Blizzard data (gear check
  with gear, enchants and talents; roster link on approval; WarcraftLogs
  optional). Applications page done (list, detail with gear/stats,
  approve/deny/reset/refresh/archive). Next: WarcraftLogs parses on
  applications and the raid features once WarcraftLogs covers Forever.
- Phase 4 (remaining): Raid summaries, Raid logs, Attendance, Tier recap,
  Records, and the raid cache tools (`/raidsummary-refresh-cache`,
  `/raidsummary-refresh-report`). These all depend
  on TBC-era data sources, so plan them together with the WoW Forever data
  model rather than polishing what's there.
- Phase 5: Public pages. Kept last on purpose: the dashboard is only
  reachable over an SSH tunnel, so these need a hosting/security decision
  (exposing part of it to the internet, a Viewer level), and what they'd
  show is mostly Phase 4 data.

Access is moderator-only for now; the permission model (Viewer level,
public pages) must allow opening it up later.

## Decisions already made (don't re-ask)

- Single Discord server per bot instance.
- No scheduled announcements, and no private approver comments on
  applications, for now.
- Audit log privacy: record who did what, where and when, never private
  text (applicant notes, announcement bodies, modal contents). Exceptions:
  settings changes keep old and new values (for history and revert), and
  announcements keep their own version history.
- `config/deployment.py` holds defaults only; the dashboard's saved values
  win. Each setting has "Reset to default".
- The dashboard theme: dark, dense, left sidebar with small uppercase group
  labels, stat tiles, segmented tabs, class colours for character names.
  Inspired by oxm.gg/pugs, not a copy of it.
- The owner prefers being walked through server-side steps one command at
  a time, with the exact commands to run.
