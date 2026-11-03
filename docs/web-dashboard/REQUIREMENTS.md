# Crusader Web Dashboard: Requirements (DRAFT v0.1)

Status: **draft for discussion**. Items marked **[DECIDE]** need an answer
from the guild/owner before the requirement is final. Items are tagged with a
priority:

- **P0**: required for the first usable release
- **P1**: full feature set (target)
- **P2**: nice to have / later

---

## 1. Goals

1. Manage everything the bot can do today from a browser, without slash
   commands or editing `config/deployment.py` and restarting.
2. Get past Discord UI limits: a real announcement builder with a live
   preview, no 4000-character modal ceilings, no "paste loot in a reply
   within 5 minutes" workarounds, proper file uploads.
3. Make bot activity visible: a searchable audit log, application history,
   raid and attendance data in one place.
4. A modern, dark, gaming-style site inspired by (not copied from)
   oxm.gg/pugs.

### Non-goals (for now)

- Supporting more than one Discord server (the bot is single-guild by
  design). **[DECIDE]** confirm.
- Replacing Discord as the place members interact. Members still apply,
  vote, and so on in Discord.
- Editing secrets (bot token, WCL client secret) from the browser.

---

## 2. Current state (what the website has to work with)

| Area | Today | Impact on the dashboard |
|---|---|---|
| Runtime | One Python process (`bot.py`, discord.py 2.6, aiohttp already a dependency). | The bot can host its own internal HTTP API with no new framework. |
| Config | Secrets and IDs in `.env`; everything else as Python constants in `config/deployment.py` (~800 lines: feature flags, roles, tiers, tag IDs, attendance thresholds, poll defaults, etc.). | Settings must move to a store the bot can reload at runtime, with `deployment.py` as the default values. |
| Data | JSON files loaded into memory per cog (`applications.json`, `announcements.json`, `polls.json`, `raid_summary_store.json`, `tier_retrospective_store.json`, WCL/Wowhead/Blizzard caches). | A second process must **not** write these files directly, because the bot would overwrite the changes. Either all writes go through the bot, or storage moves to SQLite (`storage.py` already anticipates this). |
| Audit log | Tab-separated text file `audit.log`, rotated at 5 MB. | Needs structured storage (SQLite table) to filter, search and paginate. |
| Announcements | Components V2 `Container` with `Separator`s, built from title + body with `---` splits. | The web builder can expose more of Components V2 (images, galleries, link buttons, accent colour) as long as the bot renders it. |
| Permissions | `MOD_ROLE_ID` / Manage Roles, `APPROVER_ROLE_ID`, `ORGANIZER_ROLE_ID`, `REGULAR_ROLE_ID`. | Reuse the same roles for web access, so nobody manages two permission systems. |

---

## 3. Architecture requirements

- **A-1 (P0)** The bot process exposes an internal REST + WebSocket API
  (aiohttp). Every write the dashboard makes goes through the bot, which
  owns the data and the Discord connection. One source of truth, no file
  races.
- **A-2 (P0)** Migrate storage from JSON files to SQLite behind the
  existing `ApplicationStore` interface, with a one-time import of existing
  JSON data. Caches (WCL, Wowhead, Blizzard) may stay as JSON at first.
- **A-3 (P0)** Settings layer: dashboard-editable settings are stored in
  SQLite; values not overridden fall back to `config/deployment.py`.
  Changes apply live without a restart where possible. Where a restart or
  cog reload is needed (feature toggles), the UI says so and offers a
  "Reload feature" action.
- **A-4 (P0)** Frontend: single-page app (proposed: React + TypeScript +
  Vite + Tailwind CSS), served as static files by the bot's web server or
  by a reverse proxy.
- **A-5 (P0)** Live updates over WebSocket: the bot pushes events (new
  audit entry, new application, poll vote, bot status) so pages update
  without reloading.
- **A-6 (P1)** The bot's API uses the same code paths as the slash
  commands. For example, "Approve" on the web calls the same function as
  the Approve button, so behaviour (DMs, role assignment, nickname) stays
  identical.
- **A-7 (P0)** Everything a user does on the website is written to the
  audit log, with source `web` (versus `discord`).

---

## 4. Access control and authentication

- **AUTH-1 (P0)** Log in with Discord (OAuth2, `identify` scope). No
  separate passwords.
- **AUTH-2 (P0)** Access is decided by the user's **current** roles in the
  guild, checked server-side on every request (cached briefly, about 60 s),
  so removing a role in Discord removes web access.
- **AUTH-3 (P0)** Dashboard permission levels, mapped to Discord roles:

  | Level | Default Discord mapping | Can |
  |---|---|---|
  | Viewer **[DECIDE]** | Regular? Organizer? | Read-only pages (raid stats, records, attendance?) |
  | Moderator | `MOD_ROLE_ID` or Manage Roles | Everything operational: announcements, polls, raid summaries, attendance, audit log |
  | Approver | `APPROVER_ROLE_ID` | Approve, deny or reset applications |
  | Admin | Server owner + a configurable role or user list | Settings, feature toggles, maintenance tools, permission mapping |

- **AUTH-4 (P1)** Admins can edit the role-to-level mapping in the UI.
- **AUTH-5 (P0)** Secure sessions: HttpOnly, Secure, SameSite cookies;
  CSRF protection; session expiry (for example 7 days) and logout
  everywhere.
- **AUTH-6 (P2)** Public pages with no login (for example raid records,
  tier recaps, a guild "about/apply" page). **[DECIDE]** wanted or not.

---

## 5. Functional requirements by area

### 5.1 Home / Overview (P0)

- Bot status: online/offline, gateway latency, uptime, running version
  (git commit), loaded features.
- At-a-glance cards: pending applications, active polls, last raid summary,
  next auto-summarize run (`RAID_LOG_AUTO_SUMMARIZE_TIME`), attendance
  status.
- Recent activity feed (latest audit entries, live).
- Warnings panel: misconfigured features (the startup dependency warnings
  `bot.py` already logs), missing channel or role IDs, API errors (WCL,
  Wowhead, Blizzard).

### 5.2 Configuration (P0 core, P1 complete)

- **CFG-1** Every non-secret setting in `config/deployment.py` is editable,
  grouped into sections: General and Logging, Features, Channels, Roles,
  Applications, Announcements, Polls, Attendance, Raid Summary and Tiers,
  Raid Logs automation, Audit Log.
- **CFG-2** Channel, role, forum-tag and emoji fields use **pickers** filled
  from the live guild (names and icons, not raw IDs). An ID can still be
  pasted.
- **CFG-3** Validation before saving: the type is right, the
  channel/role/tag exists, the bot has the permissions it needs in that
  channel, and feature dependencies hold (for example Raid Logs needs Raid
  Summary and Attendance).
- **CFG-4** Feature toggles show their dependencies and what the toggle
  disables.
- **CFG-5 (P1)** Change history per setting (who, when, old value, new
  value) with one-click revert.
- **CFG-6 (P1)** Tier management: current, previous and future tiers,
  sub-instances, banners (image upload or URL), forum tag mapping, excluded
  encounters and trash fights.
- **CFG-7 (P1)** Export and import of the whole configuration as JSON
  (backup, or moving to a new server).
- **CFG-8** Secrets in `.env` show only as "set / not set", never their
  values.

### 5.3 Announcement Builder (P0 core, P1 complete)

- **ANN-1 (P0)** Editor with a **live Discord-accurate preview** side by
  side (desktop) or as tabs (mobile): Discord markdown (headers, bold,
  italics, lists, quotes, code, spoilers, links), `---` section dividers,
  and custom emoji, role, channel and user mentions rendered as they will
  appear.
- **ANN-2 (P0)** Emoji picker (Unicode, guild and bot application emoji),
  plus a mention picker for roles and channels.
- **ANN-3 (P0)** Publish to a chosen channel; save as draft; edit a
  published announcement (it updates in Discord); full list of drafts and
  published announcements with who did what and when.
- **ANN-4 (P1)** Block-based composition built on Components V2: text
  section, divider, image or gallery (upload), section with thumbnail, link
  buttons, accent colour.
- **ANN-5 (P1)** **Scheduled publishing** (date, time and time zone),
  with a cancel option.
- **ANN-6 (P1)** Templates (for example "Raid reminder", "Loot rules
  update") and duplicating a previous announcement.
- **ANN-7 (P1)** Optional role ping on publish, with a confirmation step.
- **ANN-8 (P1)** "Send test to sandbox channel" keeps today's real-render
  check.
- **ANN-9 (P2)** Version history for each announcement, plus restore.
- **ANN-10 (P2)** Co-editing presence ("Mike is editing this draft").
- Constraint: Discord limits (4000 characters of text per message,
  component counts) are enforced in the editor with counters, and the
  content is split the same way the bot splits it.

### 5.4 Audit Log (P0)

- **AUD-1** Table of every action: time (shown in the viewer's time zone),
  user (avatar and name), source (Discord or web), type (slash command,
  button, select, modal, context menu, web action), action name, channel,
  details.
- **AUD-2** Filters: user, action type, command or feature, channel, date
  range, source; free-text search; shareable filter URLs.
- **AUD-3** Live tail (new entries stream in), pagination or infinite
  scroll.
- **AUD-4** Export of the current filtered view to CSV.
- **AUD-5** Clicking a row deep-links to the related entity where there is
  one (application, poll, announcement, Discord message).
- **AUD-6 (P1)** Retention setting (for example keep 1 year), replacing
  size-based rotation. Import the existing `audit.log` files on migration.
- Privacy rule unchanged: modal field contents (notes, announcement text)
  are not stored in the audit log. **[DECIDE]** confirm this still applies
  to web actions (for example whether to log the announcement diff).

### 5.5 Applications (P1)

- List with tabs for Pending, Approved, Denied and Archived; search by
  character or Discord user.
- Detail view: everything on the review card (class, spec and role icons,
  WCL tier performance, per-boss breakdown, note, screenshots in a
  lightbox, "denied before" warning) plus the applicant's full application
  history.
- Approve, Deny and Reset (Approver level), with identical side effects to
  the Discord buttons (role, nickname, DM, card update).
- Archive action (same as `/gearcheck archive`).
- Optional: an internal comment thread per application, visible only to
  approvers. **[DECIDE]**

### 5.6 Polls (P1)

- Create poll form exposing every option `/poll create` has, with the
  `POLL_*` defaults pre-filled, and a preview.
- Active and closed poll list; live results (bar chart); voters list when
  the poll is public.
- Every Manage action: open or close voting, reopen adding, add or remove
  options, remove the deadline, toggle settings (with the existing
  privacy-only-off rule enforced).

### 5.7 Raid Summaries and Raid Logs (P1)

- List of posted summaries (tier, date, clear status, link to the forum
  thread).
- **New summary** wizard: pick a report from recent logs or paste a link,
  tier, clear status, raid type, note, media link, and loot as a paste
  **or a file upload** with no length limit.
- Edit note or media; add or replace loot; regenerate; refresh one report;
  refresh a tier cache; merge weeks.
- Records browser: fastest boss kills, fastest instance clears, personal
  best parses, with history.
- Raid Logs feed: incoming reports, tag status, auto-summarize schedule and
  last run result.

### 5.8 Attendance (P1)

- Overview table matching the pinned Discord overview (who meets the
  threshold), with the refresh action.
- Manage tracked logs (add or remove), alt-to-main links, main-name
  overrides and excused players, all as forms and tables instead of
  subcommands.
- Per-member drill-down: resolved main, alts, attended logs.

### 5.9 Tier Recap (P1)

- Generate a recap for a tier, preview it, edit it in the Announcement
  Builder, publish.

### 5.10 Emoji (P2)

- Add emoji from Wowhead links (same as `/add-emoji`); browse the bot's
  application emoji and the auto-provisioned item icons.

### 5.11 Maintenance (P1, Admin only)

- Buttons for what the debug commands do today (Blizzard, spell and item
  tests, Wowhead cache wipe, bulk import), so `DEBUG_COMMANDS_ENABLED` no
  longer has to be flipped.
- Re-sync slash commands, reload a feature, restart the bot.
- Live console log viewer with a level filter (replaces SSHing in to read
  logs).
- Download a backup (database and config).

### 5.12 Notifications (P2)

- Optional browser notifications for a new application or a failed
  auto-summarize.

---

## 6. Design and UX requirements

- **UX-1** Dark theme first, gaming/esports aesthetic inspired by
  oxm.gg/pugs without copying it. **[DECIDE]** The site could not be
  fetched from this environment, so the specific elements to borrow need to
  come from you (see the questions below).
- **UX-2** Proposed direction (to confirm):
  - Near-black background (for example `#0b0d12`) with slightly lighter
    elevated cards and a subtle border.
  - One strong accent colour (guild colour, for example crusader gold or
    red), plus WoW class colours for any character name.
  - A clean geometric sans-serif typeface (for example Inter or Geist),
    with a condensed display face for headings.
  - Soft glows and gradients on key elements, rounded cards, and
    micro-animations on hover and state changes.
  - Left sidebar navigation (collapses on mobile), top bar with a guild
    switcher placeholder, bot status pill and user menu.
  - Data-dense tables with sticky headers; skeleton loading states;
    toasts for results.
- **UX-3** Fully responsive; every P0 page works on a phone.
- **UX-4** WoW-native touches: class and spec icons, item quality colours
  on loot, Wowhead tooltips on item links.
- **UX-5** Accessibility: keyboard navigation, visible focus, WCAG AA
  contrast.
- **UX-6** Command palette (Ctrl+K) to jump to any page or action (P2).
- **UX-7** Optional light theme (P2).

---

## 7. Non-functional requirements

- **NF-1 Security**: HTTPS only; the bot API is never exposed without
  auth; role checks on the server, never only in the UI; rate limiting on
  login and write endpoints; uploads validated by type and size; no secrets
  sent to the browser.
- **NF-2 Reliability**: if the web server crashes it must not take the bot
  down (it runs in its own task with error isolation). If the dashboard is
  down, the bot keeps working.
- **NF-3 Performance**: pages load in under 1 s on a normal connection;
  audit search returns in under 500 ms for about 1 million rows.
- **NF-4 Backups**: daily automatic SQLite backup with N days of
  retention.
- **NF-5 Deployability**: a single `docker compose up` (bot + reverse
  proxy with automatic HTTPS), or a documented non-Docker setup.
  **[DECIDE]** depends on hosting.
- **NF-6 Maintainability**: the existing Discord commands keep working.
  The dashboard is an addition, not a replacement.

---

## 8. Setup prerequisites (for the later setup phase)

1. Somewhere the bot and the website can run with a public HTTPS address
   (VPS, home server with a tunnel, or similar). **[DECIDE]**
2. A domain or subdomain (for example `bot.yourguild.com`), or a free
   tunnel hostname (for example Cloudflare Tunnel). **[DECIDE]**
3. In the Discord Developer Portal: copy the OAuth2 Client Secret and add
   a redirect URL (`https://<domain>/auth/callback`).
4. New `.env` entries: `DISCORD_CLIENT_SECRET`, `WEB_BASE_URL`,
   `WEB_SESSION_SECRET`, `WEB_ADMIN_USER_IDS`.

---

## 9. Proposed delivery phases

| Phase | Contents |
|---|---|
| 0: Foundation | SQLite migration, settings layer, bot API server, Discord login, permission levels, app shell and theme |
| 1: Visibility | Overview, Audit Log (with import of the old log), Configuration (core) |
| 2: Announcements | Builder, preview, drafts, publish and edit, then scheduling and templates |
| 3: Members | Applications, Polls |
| 4: Raiding | Raid Summaries, Raid Logs, Attendance, Tier Recap, records |
| 5: Polish | Maintenance tools, emoji, public pages, notifications, command palette |

---

## 10. Open questions

1. **Hosting**: where does the bot run today (your PC, a VPS, a Raspberry
   Pi, a bot host such as PebbleHost)? Do you have a domain?
2. **Who gets access**: only mods/approvers/admins, or also read-only for
   raiders? Any **public** (no-login) pages, such as records, recaps or an
   apply page?
3. **Theme**: which parts of oxm.gg/pugs do you like (colours, layout,
   card style, fonts)? Do you have a guild colour or logo? (The site is
   blocked from this environment: either allow `oxm.gg` in the
   environment's network settings or share a screenshot.)
4. **Scope of the first release**: is the phase order above right, or is
   something more urgent (for example the announcement builder first)?
5. **Scheduled announcements**: needed?
6. **Application comments**: do approvers want private discussion per
   application on the site?
7. **Audit privacy**: keep "never store text contents" for web actions
   too?
8. **Settings precedence**: once the dashboard exists, should
   `config/deployment.py` stay as defaults only (recommended), or remain
   the source of truth?
