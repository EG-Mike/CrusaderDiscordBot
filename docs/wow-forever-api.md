# Blizzard's API for WoW Forever

Blizzard hasn't documented a Forever API. This page collects what is known
from probing it (community notes by M. Huijskens, checked by the bot's own
probe on 2026-11-04 before launch and again after launch on 2026-11-05). Re-check with
`scripts/probe_blizzard_api.py` (see the end of this page) whenever
something changes; Blizzard opens things up without notice.

## Namespaces and statuses

| Namespace | Status |
|---|---|
| `static-forever-{region}` | Open since launch maintenance (Nov 4). An alias: the `battlenet-namespace` response header names the real one (`static-1.60.1_70399-forever-eu`). |
| `dynamic-forever-{region}` | Open, but **realms disappeared at launch**: `realm/index`, `realm/{id or slug}` (all the pre-launch ids) are 404 and realm search returns 0 results, in eu and us. Regions still answer. Connected realms, auctions, PvP and the WoW token are 404. |
| `profile-forever-{region}` | Open for a player's own account profile (user token). Public character and guild lookups were still 404 after launch. |

Regions: `eu`, `us`, `kr`, `tw` on the usual `{region}.api.blizzard.com`
hosts. Korea had no realms in the index yet.

How to read a status:

- **403**: the namespace doesn't exist (or isn't open), e.g. `dynamic-bogus-eu`.
- **404**: the namespace exists but nothing is at that path. A route that
  doesn't exist at all is also 404, in any namespace.
- **200**: open.

The bot's namespaces, locale and character lookup paths are settings
(Settings, General, Integrations), so a change on Blizzard's side is a
settings change, not a code change.

## What answers

- **Static (200)**: classes (9), specs (27), races (10, including High
  Order Skyborne 95 and Windshaper Skyborne 96), professions (13, plus a
  "[DNT]" test entry the bot hides). Specs carry **no role** (tank, healer,
  DPS), unlike retail, so the roster asks members for their role. Items and item media (icons on
  `render.worldofwarcraft.com/forever/...`), item sets, achievements (176),
  mounts, titles, reputations, talent trees, guild crests. Journal
  instances/encounters exist but are empty. Some spells and creatures by id
  are 404.
- **Dynamic (200)**: `region/index` (patch_string 1.60.1). Before launch
  also `realm/index` and `realm/{slug or id}` (26 EU realms such as
  `camwow-eu5-realm-1-pve`, id 6855); since launch those are 404. Realm
  `type` was the ruleset (Normal, PvP, RP, RP-PvP), and the index also
  listed service realms (`...-web`, `-gmss-`, `-partner`, ...). The bot
  copes without realms: the account profile's protected-character link
  still carries the realm id.
- **Account profile (user token, 200)**: `/profile/user/wow` lists the
  player's characters with `name` (first name), `surname`, `id`, class,
  race, gender, faction and `level`. There is **no realm object**: the realm
  id is only in the protected-character link
  (`/profile/user/wow/protected-character/6855-716333` = realm 6855,
  character 716333). Characters appear only after logging into the game and
  out again; an account without Forever characters answers 404.
- **Not yet (404)**: `/profile/wow/character/{first}-{surname}` (the link
  the account profile itself gives), the same with a realm slug, guilds
  (`/data/wow/guild/{realm}/{name}`), protected characters and collections.

Forever names look unique per region (first name + surname), which is why
character links carry no realm.

## What the bot does with it

- `wow_api.py` reads reference data (classes, specs, races, professions,
  realms) for the dashboard pickers, parses the account profile, and tries
  `CHARACTER_LOOKUP_PATHS` in order for public lookups (none answer yet).
- Members link characters by logging in with Battle.net (scope
  `wow.profile`). That is the only way to read levels until public lookups
  open; user tokens last about 24 hours and have no refresh token.
- The Maintenance page has a Blizzard API explorer for one-off requests.

## Probing it yourself

```bash
python scripts/probe_blizzard_api.py --character "First Surname" --guild "Guild Name"
```

In the test container:

```bash
docker compose exec bot python scripts/probe_blizzard_api.py --character "First Surname"
```

Add `--login` to also read your own account profile: it prints a Battle.net
link, and you paste back the address you land on (the page itself may not
load). The redirect URL must be registered for the app at
https://develop.battle.net/access/clients. The script writes
`blizzard-probe-<time>.md` (summary) and `.json` (full answers); neither
contains keys or tokens. `--help` lists all options.

The client build is on the public version server:
https://us.version.battle.net/v2/products/wowf/versions (product `wowf`).
