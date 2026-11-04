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
  (`.github/workflows/docker-image.yml`; tag `test` for the dashboard
  branch, `latest` for `main`, for **amd64 and arm64**; the VPS is ARM).
  The VPS pulls new builds every 5 minutes. **The test environment can be
  changed freely**: breaking changes and rewrites are fine there.
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
  info/export/backup.
- `audit.py`: the audit trail (structured events in the database,
  mirrored to the `audit.log` text file); written by `cogs/audit_log.py`
  (Discord) and `webapi.server.audit` (dashboard), read by the dashboard
  and `/auditlog`.
- `system_log.py`: bot lifecycle events (start, stop with reason,
  update, unexpected stop, Discord outages) and console warnings/errors
  captured from logging, secrets redacted; `app_version.py`: which build
  is running. Console output is admin-only in the dashboard.
- `announcements.py`: announcements (drafts, publishing, edits, versions,
  templates), shared by `cogs/announcements.py` and the dashboard.
- `bot_messages.py`: the bot's standard texts (pinned explainers, DMs)
  with their defaults and `{placeholders}`; cogs call `bot_messages.embed()`
  / `.text()` instead of hardcoding text.
- `secret_store.py`: external API keys, Fernet-encrypted in the database
  (key file `secret.key` next to it). Write-only towards the browser.
- `webapi/`: aiohttp dashboard API inside the bot process: Discord OAuth
  login, DB-backed sessions, role-based access (moderator/approver/admin),
  CSRF header check, settings, integrations, setup wizard, restart.
  `server.py` wires the app; feature APIs live in their own module
  (`announcements_api.py`, `bot_messages_api.py`) using `common.py`.
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
  settings go in `config/deployment.py` (picked up by the dashboard
  automatically, with help text from the comment above each constant).
  Missing configuration must make the feature inactive with a clear
  message, never crash the bot.
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
- Phase 3: Applications, Polls.
- Phase 4: Raid summaries, Raid logs, Attendance, Tier recap, Records.
  These are the most game-version-dependent areas, so plan them together
  with the WoW Forever data model.
- Phase 5: Maintenance tools, emoji, public pages, notifications,
  command palette.

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
