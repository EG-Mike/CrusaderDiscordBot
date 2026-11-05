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
| `dynamic-forever-{region}` | Open. **Forever has rulesets instead of realms**: since launch `ruleset/index` and `ruleset/{id}` answer, while `realm/index`, `realm/{id or slug}`, connected realms and realm search are gone (404 / empty). Auction houses hang off rulesets. Regions still answer; PvP and the WoW token are 404. |
| `profile-forever-{region}` | Open for a player's own account profile (user token). Public character and guild lookups were still 404 after launch. |

Regions: `eu`, `us`, `kr`, `tw` on the usual `{region}.api.blizzard.com`
hosts.

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
- **Rulesets (dynamic, 200)**: what players pick instead of a realm.
  `ruleset/index` lists ids only; `ruleset/{id}` gives the name and a live
  `status` (`UP`/`DOWN`), plus a link to its auction houses. Ids are the
  same in every region: 1 PvP, 2 Normal, 3 Roleplay, 15 Roleplay PvP (Korea
  and Taiwan have only PvP and Normal). Checked 2026-11-05.

  ```json
  GET /data/wow/ruleset/2?namespace=dynamic-forever-eu
  {"id": 2, "name": "Normal", "status": {"type": "UP", "name": "Up"},
   "auctions": {"href": ".../data/wow/ruleset/2/auctions/?namespace=dynamic-forever-eu"}}
  ```
- **Auction houses (dynamic, 200)**: `ruleset/{id}/auctions/index` (the
  link without `index` is a 404) lists the houses of that ruleset (15 on
  EU PvP: several Alliance, Horde and Blackwater ones, ids from 110);
  `ruleset/{id}/auctions/{house}` and `.../{house}/commodities` answer with
  live data.
- **Regions (dynamic, 200)**: `region/index`, `region/{id}` (patch_string
  1.60.1). Before launch there was also a realm list (26 EU realms such as
  `camwow-eu5-realm-1-pve`, id 6855, with service realms like `...-web`);
  it was removed at launch.
- **Account profile (user token, 200)**: `/profile/user/wow` lists the
  player's characters with `name` (first name), `surname`, `id`, class,
  race, gender, faction and `level`. There is **no realm object**: an internal realm
  id is only in the protected-character link
  (`/profile/user/wow/protected-character/6855-716333` = realm 6855,
  character 716333). Characters appear only after logging into the game and
  out again; an account without Forever characters answers 404.
- **Not yet (404)**: `/profile/wow/character/{first}-{surname}` (the link
  the account profile itself gives), guilds (`/data/wow/guild/{name}`,
  and the retail-style `/data/wow/guild/{realm}/{name}`), protected
  characters and collections.

Forever names look unique per region (first name + surname), which is why
character links carry no realm.

**Likely reason public lookups are 404: the level-10 rule.** In the modern
game's API (and the official Armory), a character's public profile only
exists from level 10, after it has logged out at that level, and it drops
off after long inactivity. All characters checked so far were below 10, so
the 404s may simply be this rather than lookups being closed. The account
profile (user token) lists characters of every level.

Test character for checks: **Eggmike Thewhite**, EU, PvP ruleset (id 1).
On 2026-11-05 every lookup variant (`{first}-{surname}`, first name only,
with `pvp` or `1` in front, the pre-launch realm slugs, eu and us) was 404
while it was below level 10. Re-check once it's level 10+ and logged out:

```bash
python scripts/probe_blizzard_api.py --region eu --character "Eggmike Thewhite"
```

## What the bot does with it

- `wow_api.py` reads reference data (classes, specs, races, professions,
  rulesets) for the dashboard pickers, parses the account profile, and tries
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
