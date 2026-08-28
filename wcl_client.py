"""
Minimal WarcraftLogs (v2 GraphQL) client using the OAuth client-credentials flow.

Docs: https://www.warcraftlogs.com/api/docs
- Token URL: https://www.warcraftlogs.com/oauth/token (shared across all game versions)
- API URL:   https://fresh.warcraftlogs.com/api/v2/client (confirmed working host
             for Fresh/Anniversary realms - the main www.warcraftlogs.com host
             does NOT return Fresh characters, since each game-version site
             serves its own database)
"""

import time
import asyncio
import logging
from collections import Counter
import aiohttp

import config
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.wcl")

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://fresh.warcraftlogs.com/api/v2/client"

# A character counts as Tank/Healer for get_report_role_composition() only
# if they filled that role in at least this fraction of the fights they
# appeared in (handles hybrids who tank/heal some pulls and DPS others) -
# otherwise they're counted as DPS.
ROLE_THRESHOLD = 0.70

_ROLE_KEY_MAP = {
    "tanks": "tank", "tank": "tank",
    "healers": "healer", "healer": "healer",
    "dps": "dps",
}


def _normalize_role(role_key: str) -> str:
    return _ROLE_KEY_MAP.get((role_key or "").lower(), "dps")

# Every key get_report_summary()'s cached entry must have. Checked on every
# cache read (see get_report_summary) rather than just checking for
# "fights" - a per-report cache is persisted to disk (wcl_report_cache.json)
# and outlives the process, so an entry cached by an older version of this
# method (before some field existed) would otherwise be returned as-is,
# missing that field, and crash whatever downstream code expects it (this
# happened for real: "healing_done" was added after some reports were
# already cached). An incomplete entry just gets transparently re-fetched
# once instead.
_SUMMARY_KEYS = {
    "zone", "start_time", "end_time", "fights", "kill_counts",
    "kill_fight_roles", "parses", "deaths", "damage_done", "healing_done",
    "activity", "potion_casts", "interrupts", "dispels",
}

CHARACTER_QUERY = """
query CharacterLookup($name: String!, $serverSlug: String!, $serverRegion: String!) {
  characterData {
    character(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      name
      classID
      level
      hidden
      guilds {
        name
      }
      server {
        name
        region { name }
      }
      zoneRankings
    }
  }
}
"""

# Ground-truth class id -> name map, fetched from WCL itself rather than
# hardcoded - retail/classic class IDs don't necessarily match, so we ask the
# API directly instead of guessing.
CLASS_MAP_QUERY = """
query ClassMap {
  gameData {
    classes {
      id
      name
    }
  }
}
"""

# Fetches per-encounter rankings for a specific zone.
CHARACTER_TIER_QUERY = """
query CharacterTierLookup($id: Int!, $zoneId: Int!) {
  characterData {
    character(id: $id) {
      zoneRankings(zoneID: $zoneId)
    }
  }
}
"""

# --- attendance tracking (report-level, not character-level) ---

REPORT_PLAYER_DETAILS_QUERY = """
query ReportPlayers($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      playerDetails(fightIDs: $fightIDs)
    }
  }
}
"""

# --- raid summary (report-level, reuses the same cached fetch as attendance) ---
#
# One request gets every fight (including wipes - kill: false - and trash,
# kill: null, which attendance ignores but pull-count/first-kill-badge
# reporting needs) plus the whole report's parse rankings in a single shot,
# rather than the fight-by-fight playerDetails loop that's needed for
# attendance's roster/kill-count data. get_report_summary() below fetches
# this ONCE per report and merges it with the existing kill_counts fetch
# into one cache entry - see its docstring.
REPORT_FIGHTS_AND_RANKINGS_QUERY = """
query ReportFightsAndRankings($code: String!) {
  reportData {
    report(code: $code) {
      title
      startTime
      endTime
      zone { id name }
      fights {
        id
        name
        encounterID
        kill
        difficulty
        startTime
        endTime
        bossPercentage
        fightPercentage
      }
      rankings
    }
  }
}
"""

# dataType: Deaths, across every fight at once (a single table query, not
# one per fight) - who died and how many times, for the "fun stats" section.
REPORT_DEATHS_QUERY = """
query ReportDeaths($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Deaths)
    }
  }
}
"""

# dataType: DamageDone, across every fight at once (bosses AND trash, same
# as WCL's own "Overall" damage-done ranking view) - the "top damage"
# section.
REPORT_DAMAGE_DONE_QUERY = """
query ReportDamageDone($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: DamageDone)
    }
  }
}
"""

# dataType: Healing, across every fight at once - the "highest healing done"
# raid MVP line.
REPORT_HEALING_QUERY = """
query ReportHealing($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Healing)
    }
  }
}
"""

# dataType: Summary, across every fight at once - the same table that backs
# WCL's own combined (role-agnostic) "Summary" tab, whose entries carry each
# player's activeTime - the "Top Activity %" fun-stat leaderboard divides
# that by the raid's total selected-fight duration. Unlike DamageDone/
# Healing (role-specific), this is the one table that gives every raider,
# tank/healer/DPS alike, a single comparable activity number.
REPORT_ACTIVITY_QUERY = """
query ReportActivity($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Summary)
    }
  }
}
"""

# dataType: Casts, across every fight at once - per-player cast counts
# broken down by ability (same nested-breakdown shape DamageDone/Healing
# entries use for their own per-ability totals). Used for the "Top potion
# users" leaderboard (config.TRACKED_POTIONS, matched by ability name) -
# not filtered to a specific abilityID here since matching by name against
# the full per-player breakdown avoids needing an exact-rank spell ID (see
# config.py's TRACKED_POTIONS comment).
REPORT_CASTS_QUERY = """
query ReportCasts($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Casts)
    }
  }
}
"""

# dataType: Interrupts / Dispels, across every fight at once - flat per-
# player totals, same shape as Deaths/DamageDone - the "Top Interrupters"/
# "Top Dispellers" fun-stat leaderboards.
REPORT_INTERRUPTS_QUERY = """
query ReportInterrupts($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Interrupts)
    }
  }
}
"""

REPORT_DISPELS_QUERY = """
query ReportDispels($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Dispels)
    }
  }
}
"""

# --- buff/debuff uptime tracking (cogs/raid_summary.py's "Buff/Debuff
# Uptime" section) ---
#
# dataType: Debuffs/Buffs, unfiltered by abilityID - returns every aura's
# own totalUptime (ms) within the given fights, one entry per distinct
# ability. config.TRACKED_DEBUFFS/TRACKED_BUFFS match against these entries'
# NAME client-side (see that file's comment on why: matching by exact spell
# ID would miss whichever rank a given raid actually used). hostilityType
# picks which side's auras this reads: Enemies for a debuff living on the
# boss, Friendlies for a buff living on a raid member.
REPORT_DEBUFFS_QUERY = """
query ReportDebuffs($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Debuffs, hostilityType: Enemies)
    }
  }
}
"""

REPORT_BUFFS_QUERY = """
query ReportBuffs($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Buffs, hostilityType: Friendlies)
    }
  }
}
"""

# Same two tables, viewed "by source"/"by target" instead of the default
# per-ability view - regroups entries by PLAYER instead, each carrying a
# nested per-ability uptime breakdown, so the raid summary can call out
# whichever raider contributed the most of a tracked debuff/buff's uptime
# (the applying warrior/warlock for a debuff kept on the boss; the paladin's
# target - usually the tank - actually wearing a buff). The `viewBy`
# argument and this per-player nested shape mirror how WCL's own UI lets you
# flip a Buffs/Debuffs tab between "by ability" and "by source"/"by target",
# but - like GUILD_ZONE_RANKINGS_QUERY above - this could not be verified
# against a live report from this sandbox; see get_report_aura_uptime's
# docstring for how a shape surprise here degrades gracefully.
REPORT_DEBUFFS_BY_SOURCE_QUERY = """
query ReportDebuffsBySource($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Debuffs, hostilityType: Enemies, viewBy: Source)
    }
  }
}
"""

REPORT_BUFFS_BY_TARGET_QUERY = """
query ReportBuffsByTarget($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Buffs, hostilityType: Friendlies, viewBy: Target)
    }
  }
}
"""

# Guild-level (not report-level) - a guild's speed/execution rankings across
# every boss in a zone, i.e. "how does our kill of Boss X compare to other
# guilds on the server/region". Best-effort: this JSON shape could not be
# verified live (see wcl_client's module docstring), so get_guild_zone_rankings()
# below parses it defensively and returns None on anything unexpected rather
# than raising.
#
# zoneRanking (unlike Character.zoneRankings above, a JSON scalar that
# needs no sub-selection) is a typed GuildZoneRankings! object - querying
# it bare 400'd live with "must have a sub selection". The sub-selection
# below picks the same field names get_guild_zone_rankings() already
# parses (bestPerformanceAverage / rankings[].rank/serverRank/regionRank/
# rankPercent/encounter.id/encounter.name) - those were themselves copied
# from the confirmed-working Character.zoneRankings JSON shape used
# elsewhere in this file, so it's an educated guess rather than a
# confirmed schema, same caveat as before.
GUILD_ZONE_RANKINGS_QUERY = """
query GuildZoneRankings($name: String!, $serverSlug: String!, $serverRegion: String!, $zoneId: Int!) {
  guildData {
    guild(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      name
      zoneRanking(zoneId: $zoneId) {
        bestPerformanceAverage
        rankings {
          rank
          serverRank
          regionRank
          rankPercent
          encounter {
            id
            name
          }
        }
      }
    }
  }
}
"""


class WarcraftLogsClient:
    def __init__(self, client_id: str, client_secret: str, report_cache_path: str = "wcl_report_cache.json"):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._token_expires_at = 0
        self._class_map = None  # cached id -> name, fetched once from the API

        # Caches each report's raw per-player kill-fight tally, keyed by
        # report code - a WCL report's fight/kill data doesn't change once
        # uploaded, so once we've paid the cost of walking every kill fight
        # for a report, we never need to do it again. Deliberately caches
        # the raw counts (not a pre-filtered attendance set), so changing
        # ATTENDANCE_MIN_KILLS_PER_LOG later still works correctly against
        # already-cached data - see get_report_attendance() below. To force
        # a re-fetch for a specific report (e.g. if you suspect it was
        # re-uploaded/edited), delete that file or its entry manually.
        self._report_cache = ApplicationStore(path=report_cache_path)

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        # Build the Basic auth header explicitly (rather than aiohttp's
        # `auth=` kwarg, which triggers a deprecation warning on newer
        # aiohttp versions) using BasicAuth's own encoder.
        auth_header = aiohttp.BasicAuth(self.client_id, self.client_secret).encode()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": auth_header},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        return self._token

    async def _get_class_map(self) -> dict:
        """Fetches and caches the real classID -> name map from WCL itself."""
        if self._class_map is not None:
            return self._class_map

        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                json={"query": CLASS_MAP_QUERY},
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

        if "errors" in payload:
            raise RuntimeError(f"WarcraftLogs API error: {payload['errors']}")

        classes = payload.get("data", {}).get("gameData", {}).get("classes") or []
        self._class_map = {c["id"]: c["name"] for c in classes}
        return self._class_map

    async def get_character(self, name: str, server_slug: str, server_region: str):
        """
        Returns a dict with parsed character info, or None if the character
        wasn't found on WarcraftLogs (which can also just mean they have no
        logs uploaded yet).
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                json={
                    "query": CHARACTER_QUERY,
                    "variables": {
                        "name": name,
                        "serverSlug": server_slug,
                        "serverRegion": server_region,
                    },
                },
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

        if "errors" in payload:
            raise RuntimeError(f"WarcraftLogs API error: {payload['errors']}")

        char = payload.get("data", {}).get("characterData", {}).get("character")
        if not char:
            return None

        class_map = await self._get_class_map()
        return self._parse_character(char, server_region, server_slug, class_map)

    def _parse_character(self, char: dict, server_region: str, server_slug: str, class_map: dict) -> dict:
        class_name = class_map.get(char.get("classID"), "Unknown class")

        guilds = char.get("guilds") or []
        guild_name = guilds[0]["name"] if guilds else "None"

        zr = char.get("zoneRankings") or {}
        best_perf = zr.get("bestPerformanceAverage")
        median_perf = zr.get("medianPerformanceAverage")

        profile_url = (
            f"https://fresh.warcraftlogs.com/character/{server_region}/"
            f"{server_slug}/{char['name']}"
        )

        return {
            "id": char["id"],
            "name": char["name"],
            "class_name": class_name,
            "level": char.get("level"),
            "hidden": char.get("hidden", False),
            "guild_name": guild_name,
            "best_performance_average": best_perf,
            "median_performance_average": median_perf,
            "profile_url": profile_url,
        }

    async def get_tier_data(self, character_id: int, zone_id: int):
        """
        Returns a dict with:
          - best_performance_average / median_performance_average (overall, for this zone)
          - per_boss: list of {encounter_id, encounter_name, kills, best_percent}
          - total_kills: sum of kills across all encounters in this zone
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        variables = {"id": character_id, "zoneId": zone_id}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                json={"query": CHARACTER_TIER_QUERY, "variables": variables},
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

        if "errors" in payload:
            raise RuntimeError(f"WarcraftLogs API error: {payload['errors']}")

        char = payload.get("data", {}).get("characterData", {}).get("character")
        if not char:
            return None

        zr = char.get("zoneRankings") or {}
        rankings = zr.get("rankings") or []

        per_boss = []
        total_kills = 0
        spec_votes = Counter()
        for r in rankings:
            encounter = r.get("encounter") or {}
            kills = r.get("totalKills", 0) or 0
            total_kills += kills
            per_boss.append({
                "encounter_id": encounter.get("id"),
                "encounter_name": encounter.get("name", "Unknown boss"),
                "kills": kills,
                "best_percent": r.get("rankPercent"),
            })
            spec = r.get("spec")
            if spec:
                spec_votes[spec] += kills or 1

        primary_spec = spec_votes.most_common(1)[0][0] if spec_votes else None

        return {
            "best_performance_average": zr.get("bestPerformanceAverage"),
            "median_performance_average": zr.get("medianPerformanceAverage"),
            "per_boss": per_boss,
            "total_kills": total_kills,
            "primary_spec": primary_spec,
        }

    async def _fetch_player_details(self, session, headers, report_code: str, fight_ids: list):
        """
        Shared by get_report_summary() (kill fights) and
        get_report_role_composition() below (wipe fights) - one WCL request
        per given fight, returning:
          - kill_counts: {name: fight_count} - only meaningful when
            fight_ids are kill fights (attendance's use).
          - role_tally: {name: {"tank": n, "healer": n, "dps": n, "total": n,
            "class": class_name}} - how many of THESE given fights each
            character appeared in under each role bucket, plus their class
            (first-seen - doesn't change across fights). Fight-set-agnostic
            by design, so get_report_role_composition() can call this again
            for wipe fights and merge the two tallies into a whole-report
            picture (including a name -> class map for icons elsewhere in
            a raid summary) without re-fetching the kill fights it already
            has.

        Verified against a real report (2026-07): report.fights[].kill is
        true/false/null as expected (null = trash, false = wipe, true =
        kill), and playerDetails(fightIDs: [...]) reliably returns `name`
        per player across the dps/healers/tanks buckets.
        """
        kill_counts = {}
        role_tally = {}
        for fight_id in fight_ids:
            async with session.post(
                API_URL,
                json={
                    "query": REPORT_PLAYER_DETAILS_QUERY,
                    "variables": {"code": report_code, "fightIDs": [fight_id]},
                },
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                player_payload = await resp.json()

            await asyncio.sleep(0.15)  # pacing - a full attendance run can hit
                                        # dozens of these across a 5-log window

            if "errors" in player_payload:
                log.warning(
                    "WCL error fetching playerDetails for fight %s in report %s: %s",
                    fight_id, report_code, player_payload["errors"],
                )
                continue

            report_data = (
                player_payload.get("data", {}).get("reportData", {}).get("report") or {}
            )
            details = report_data.get("playerDetails") or {}
            inner = details.get("data") if isinstance(details.get("data"), dict) else {}
            buckets = inner.get("playerDetails", {}) if inner else {}
            if not isinstance(buckets, dict):
                # Seen live: WCL returns [] here (not the expected
                # {dps/healers/tanks: [...]} dict) for some fights - a wipe
                # fight with no meaningful player data, going by when this
                # showed up. Treat as "no players for this fight" rather
                # than crashing the whole report/composition fetch.
                continue
            for role_key, role_list in buckets.items():
                if not isinstance(role_list, list):
                    continue
                for player in role_list:
                    name = player.get("name")
                    if not name:
                        continue
                    # kill_counts stays exactly as before (attendance's
                    # behavior must not change here) - every playerDetails
                    # entry counts, pets/summons included, same as always.
                    kill_counts[name] = kill_counts.get(name, 0) + 1

                    # role_tally (roster composition / class icons) is
                    # narrower: WCL's playerDetails buckets have been seen
                    # live to include non-player entries (pets/summons) -
                    # 27 "raiders" on a 25-cap raid, going by report. Those
                    # don't have a real WoW class, so filtering to the known
                    # class set (config.CLASS_EMOJI_NAMES) excludes them
                    # without needing to guess at a "is this a pet" field.
                    class_name = player.get("type")
                    if class_name not in config.CLASS_EMOJI_NAMES:
                        continue
                    tally = role_tally.setdefault(
                        name, {"tank": 0, "healer": 0, "dps": 0, "total": 0, "class": class_name}
                    )
                    tally[_normalize_role(role_key)] += 1
                    tally["total"] += 1

        return kill_counts, role_tally

    def _parse_rankings(self, raw) -> list:
        """
        Best-effort parse of Report.rankings' JSON blob into a flat list of
        {name, class, spec, rank_percent, fight_id, boss_name, role}. This
        JSON shape could not be verified against a live report from this
        sandbox (see the module docstring) - on any structural surprise this
        logs a warning and returns [] rather than raising, so an unexpected
        WCL response degrades the summary's parse-highlight section to "no
        data" instead of breaking the whole report fetch.

        Two independent sources for which boss an entry belongs to, since
        the original guess (entry["fight"] joined against fights[].id) was
        confirmed wrong in practice (every entry came back "Unknown boss"):
        entry["encounter"]["name"] if the entry carries it directly (which
        WCL's own Rankings tab groups by, so it's the more likely field to
        actually be there), AND a fight-id join as a fallback, now trying
        both a "fightID" and "fight" key rather than assuming one. Callers
        should prefer boss_name and only fall back to joining fight_id
        against their own fights list if boss_name is empty.
        """
        if not isinstance(raw, dict):
            return []
        parses = []
        try:
            for entry in raw.get("data") or []:
                fight_id = entry.get("fightID", entry.get("fight"))
                encounter = entry.get("encounter") or {}
                boss_name = encounter.get("name")
                roles = entry.get("roles") or {}
                for role_name, role_data in roles.items():
                    if not isinstance(role_data, dict):
                        continue
                    for char in role_data.get("characters") or []:
                        parses.append({
                            "name": char.get("name"),
                            "class": char.get("class"),
                            "spec": char.get("spec"),
                            "rank_percent": char.get("rankPercent"),
                            "fight_id": fight_id,
                            "boss_name": boss_name,
                            "role": role_name,
                        })
        except Exception:
            log.warning("Unexpected shape for report rankings - skipping parse highlights", exc_info=True)
            return []
        return parses

    async def _fetch_table_entries(self, session, headers, report_code: str, fight_ids: list, query: str) -> list:
        """
        Shared HTTP + unwrap for report table() queries (Deaths,
        DamageDone, ...) - one request across the given fight IDs, not one
        per fight. Returns the raw `entries` list, or [] on any failure/
        unexpected shape - see _parse_rankings' docstring, same best-effort
        reasoning applies to table() shapes too.
        """
        if not fight_ids:
            return []
        try:
            async with session.post(
                API_URL,
                json={"query": query, "variables": {"code": report_code, "fightIDs": fight_ids}},
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except Exception:
            log.warning("Failed to fetch report table for %s", report_code, exc_info=True)
            return []

        if "errors" in payload:
            log.warning("WCL error fetching report table for %s: %s", report_code, payload["errors"])
            return []

        report_data = payload.get("data", {}).get("reportData", {}).get("report") or {}
        table = report_data.get("table") or {}
        inner = table.get("data") if isinstance(table.get("data"), dict) else table
        return (inner or {}).get("entries") or []

    async def _fetch_deaths(self, session, headers, report_code: str, fight_ids: list) -> dict:
        """Best-effort {character_name: death_count} across the whole report."""
        entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, REPORT_DEATHS_QUERY)
        counts = Counter()
        try:
            for entry in entries:
                name = entry.get("name")
                if name:
                    counts[name] += entry.get("total") or entry.get("count") or 1
        except Exception:
            log.warning("Unexpected shape for deaths table - skipping", exc_info=True)
            return {}
        return dict(counts)

    async def _fetch_damage_done(self, session, headers, report_code: str, fight_ids: list) -> dict:
        """Best-effort {character_name: total_damage} across the whole
        report (bosses AND trash - same fight_ids as everything else here,
        matching WCL's own "Overall" damage-done ranking view)."""
        entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, REPORT_DAMAGE_DONE_QUERY)
        totals = Counter()
        try:
            for entry in entries:
                name = entry.get("name")
                if name:
                    totals[name] += entry.get("total") or 0
        except Exception:
            log.warning("Unexpected shape for damage-done table - skipping", exc_info=True)
            return {}
        return dict(totals)

    async def _fetch_healing_done(self, session, headers, report_code: str, fight_ids: list) -> dict:
        """Best-effort {character_name: total_healing} across the whole
        report - the "highest healing done" raid MVP line."""
        entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, REPORT_HEALING_QUERY)
        totals = Counter()
        try:
            for entry in entries:
                name = entry.get("name")
                if name:
                    totals[name] += entry.get("total") or 0
        except Exception:
            log.warning("Unexpected shape for healing table - skipping", exc_info=True)
            return {}
        return dict(totals)

    async def _fetch_activity(self, session, headers, report_code: str, fight_ids: list, total_duration_ms: int) -> dict:
        """Best-effort {character_name: activity_pct} - each player's
        activeTime (from the Summary table - see REPORT_ACTIVITY_QUERY) as a
        % of the raid's total selected-fight duration, matching WCL's own
        "Active %" column."""
        entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, REPORT_ACTIVITY_QUERY)
        result = {}
        try:
            for entry in entries:
                name = entry.get("name")
                active_ms = entry.get("activeTime")
                if name and active_ms is not None and total_duration_ms:
                    result[name] = min(100.0, active_ms / total_duration_ms * 100)
        except Exception:
            log.warning("Unexpected shape for activity summary table - skipping", exc_info=True)
            return {}
        return result

    async def _fetch_ability_cast_counts(self, session, headers, report_code: str, fight_ids: list, tracked_names: set) -> dict:
        """Best-effort {character_name: cast_count} - total casts of any
        ability in tracked_names, summed per player, from the Casts table's
        per-player "abilities" breakdown (see REPORT_CASTS_QUERY)."""
        entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, REPORT_CASTS_QUERY)
        result = {}
        try:
            for entry in entries:
                name = entry.get("name")
                if not name:
                    continue
                for ability in entry.get("abilities") or []:
                    if ability.get("name") in tracked_names:
                        result[name] = result.get(name, 0) + (ability.get("total") or 0)
        except Exception:
            log.warning("Unexpected shape for casts table - skipping", exc_info=True)
            return {}
        return result

    async def _fetch_nested_count_table(self, session, headers, report_code: str, fight_ids: list, query: str) -> dict:
        """Best-effort {character_name: total} for Interrupts/Dispels -
        confirmed live (2026-08) that these do NOT come back as a flat
        per-player list like Deaths/DamageDone: the top-level "entries" is
        a single wrapper object whose OWN "entries" list is one row per
        interrupted/dispelled ability (e.g. a boss's "Fireball" cast, or a
        dispelled "Poison Shield" buff), and each of THOSE carries a
        "details" list of the players who did it, with their own per-
        ability "total" - two levels deeper than assumed originally."""
        wrapper_entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, query)
        result = {}
        try:
            for wrapper in wrapper_entries:
                for ability_row in wrapper.get("entries") or []:
                    for detail in ability_row.get("details") or []:
                        name = detail.get("name")
                        if name:
                            result[name] = result.get(name, 0) + (detail.get("total") or 0)
        except Exception:
            log.warning("Unexpected shape for nested count table - skipping", exc_info=True)
            return {}
        return result

    async def _fetch_aura_uptime(self, session, headers, report_code: str, fight_ids: list, query: str) -> dict:
        """Best-effort {ability_name: total_uptime_ms} from an unfiltered
        Buffs/Debuffs table (see REPORT_DEBUFFS_QUERY/REPORT_BUFFS_QUERY) -
        every aura on that side (enemy/friendly), not just the tracked ones;
        matching against config.TRACKED_DEBUFFS/TRACKED_BUFFS happens in the
        caller (get_report_aura_uptime)."""
        entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, query)
        result = {}
        try:
            for entry in entries:
                name = entry.get("name")
                uptime_ms = entry.get("totalUptime")
                if name and uptime_ms is not None:
                    result[name] = result.get(name, 0) + uptime_ms
        except Exception:
            log.warning("Unexpected shape for aura uptime table - skipping", exc_info=True)
            return {}
        return result

    async def _fetch_aura_uptime_by_player(self, session, headers, report_code: str, fight_ids: list, query: str) -> dict:
        """Best-effort {ability_name: {character_name: uptime_ms}} from a
        Buffs/Debuffs table queried "by source"/"by target" (see
        REPORT_DEBUFFS_BY_SOURCE_QUERY/REPORT_BUFFS_BY_TARGET_QUERY) -
        entries become players instead of abilities, each carrying a nested
        per-ability uptime breakdown (assumed to mirror the "abilities"
        breakdown DamageDone/Healing/Casts entries already use elsewhere in
        this file - not independently verified for aura tables specifically,
        see those queries' own docstring). Returns {} on any unexpected
        shape rather than raising, same as every other best-effort parse
        here."""
        entries = await self._fetch_table_entries(session, headers, report_code, fight_ids, query)
        result = {}
        try:
            for entry in entries:
                player_name = entry.get("name")
                if not player_name:
                    continue
                for ability in entry.get("abilities") or []:
                    ability_name = ability.get("name")
                    uptime_ms = ability.get("totalUptime")
                    if ability_name and uptime_ms is not None:
                        bucket = result.setdefault(ability_name, {})
                        bucket[player_name] = bucket.get(player_name, 0) + uptime_ms
        except Exception:
            log.warning("Unexpected shape for per-player aura uptime table - skipping", exc_info=True)
            return {}
        return result

    async def get_report_summary(self, report_code: str) -> dict:
        """
        THE single per-report fetch point. Everything any feature needs from
        a WCL report - fight list (incl. wipes/trash, not just kills),
        parse rankings, deaths, damage done, the attendance kill-count
        tally, and a KILL-FIGHTS-ONLY role tally - is fetched here ONCE per
        report code and cached together, so e.g. /checkattendance and a raid
        summary generated from the same log never pay for the same WCL
        requests twice. Cached in the same self._report_cache as before (see
        __init__'s note on it) - an incomplete/outdated cache entry (missing
        any key in _SUMMARY_KEYS, e.g. from before this method existed, or
        from before a field was added to it) is treated as a cache miss and
        transparently re-fetched.

        "kill_fight_roles" is deliberately KILL FIGHTS ONLY (cheap, already
        paid for by kill_counts' own fetch) - a full-report role tally
        (needed to correctly classify hybrids who tank/heal some pulls and
        DPS others) also needs the WIPE fights' playerDetails, which costs
        one more WCL request per wipe. That's significant enough NOT to pay
        on every summary fetch (attendance never needs it) - see
        get_report_role_composition() below, which extends this tally
        on-demand instead, only when a raid summary's comp section actually
        needs it, and caches the result right alongside this report's entry.

        Returns:
          {
            "zone": {"id", "name"} or None,
            "start_time": ms epoch or None, "end_time": ms epoch or None,
            "fights": [{"id","name","encounter_id","kill","difficulty",
                        "start_time","end_time","boss_percentage","fight_percentage"}, ...],
            "kill_counts": {name: kill_fight_count},        # attendance - verified shape
            "kill_fight_roles": {name: {"tank","healer","dps","total","class"}},  # best-effort, kill fights only
            "parses": [{"name","class","spec","rank_percent","fight_id","boss_name","role"}, ...],  # best-effort
            "deaths": {name: death_count},                   # best-effort
            "damage_done": {name: total_damage},              # best-effort
            "healing_done": {name: total_healing},            # best-effort
            "activity": {name: activity_pct},                 # best-effort
            "potion_casts": {name: cast_count},               # best-effort - config.TRACKED_POTIONS combined
            "interrupts": {name: interrupt_count},            # best-effort
            "dispels": {name: dispel_count},                  # best-effort
          }
        """
        cached = self._report_cache.get(report_code)
        if cached is not None and _SUMMARY_KEYS.issubset(cached.keys()):
            return cached

        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                json={"query": REPORT_FIGHTS_AND_RANKINGS_QUERY, "variables": {"code": report_code}},
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

            if "errors" in payload:
                raise RuntimeError(f"WarcraftLogs API error: {payload['errors']}")

            report = payload.get("data", {}).get("reportData", {}).get("report")
            if not report:
                empty = {
                    "zone": None, "start_time": None, "end_time": None, "fights": [],
                    "kill_counts": {}, "kill_fight_roles": {}, "parses": [], "deaths": {},
                    "damage_done": {}, "healing_done": {}, "activity": {}, "potion_casts": {},
                    "interrupts": {}, "dispels": {},
                }
                self._report_cache.set(report_code, **empty)
                return empty

            fights = [
                {
                    "id": f["id"],
                    "name": f.get("name"),
                    "encounter_id": f.get("encounterID"),
                    "kill": f.get("kill"),
                    "difficulty": f.get("difficulty"),
                    "start_time": f.get("startTime"),
                    "end_time": f.get("endTime"),
                    "boss_percentage": f.get("bossPercentage"),
                    "fight_percentage": f.get("fightPercentage"),
                }
                for f in (report.get("fights") or [])
            ]
            all_fight_ids = [f["id"] for f in fights]
            kill_fight_ids = [f["id"] for f in fights if f["kill"]]

            kill_counts, kill_fight_roles = await self._fetch_player_details(
                session, headers, report_code, kill_fight_ids
            )
            parses = self._parse_rankings(report.get("rankings"))
            deaths = await self._fetch_deaths(session, headers, report_code, all_fight_ids)
            damage_done = await self._fetch_damage_done(session, headers, report_code, all_fight_ids)
            healing_done = await self._fetch_healing_done(session, headers, report_code, all_fight_ids)

            # activeTime is a % of the raid's total selected-fight duration
            # (bosses + trash, same fight set as damage/healing/deaths above).
            all_duration_ms = sum(
                (f["end_time"] or 0) - (f["start_time"] or 0) for f in fights
                if f["start_time"] is not None and f["end_time"] is not None
            )
            activity = await self._fetch_activity(session, headers, report_code, all_fight_ids, all_duration_ms)
            potion_casts = await self._fetch_ability_cast_counts(
                session, headers, report_code, all_fight_ids, set(config.TRACKED_POTIONS)
            )
            interrupts = await self._fetch_nested_count_table(session, headers, report_code, all_fight_ids, REPORT_INTERRUPTS_QUERY)
            dispels = await self._fetch_nested_count_table(session, headers, report_code, all_fight_ids, REPORT_DISPELS_QUERY)

            zone = report.get("zone")
            summary = {
                "zone": {"id": zone.get("id"), "name": zone.get("name")} if zone else None,
                "start_time": report.get("startTime"),
                "end_time": report.get("endTime"),
                "fights": fights,
                "kill_counts": kill_counts,
                "kill_fight_roles": kill_fight_roles,
                "parses": parses,
                "deaths": deaths,
                "damage_done": damage_done,
                "healing_done": healing_done,
                "activity": activity,
                "potion_casts": potion_casts,
                "interrupts": interrupts,
                "dispels": dispels,
            }

        self._report_cache.set(report_code, **summary)
        return summary

    async def get_report_role_composition(self, report_code: str) -> dict:
        """
        Best-effort raid composition across the WHOLE report (not just kill
        fights) - see ROLE_THRESHOLD above: a character counts as Tank/
        Healer only if they filled that role in at least that fraction of
        the fights they appeared in this report; everyone else counts as
        DPS. Kill fights' data is reused from get_report_summary()'s own
        fetch (already paid for); only the wipe fights need a fresh fetch
        here, and that result is cached onto the same per-report cache
        entry so a second raid summary for this report never re-fetches it.

        Returns {"tanks": [name,...], "healers": [...], "dps": [...],
        "classes": {name: class_name}}, or None if the report has no
        fights. "classes" covers every character seen in ANY fight this
        report (kill or wipe) - used elsewhere in a raid summary to prefix
        names with a class icon (loot winners, damage/death leaderboards,
        etc.) without a separate lookup.
        """
        summary = await self.get_report_summary(report_code)
        if not summary.get("fights"):
            return None

        cached_entry = self._report_cache.get(report_code) or {}
        cached_composition = cached_entry.get("role_composition")
        if cached_composition is not None and "classes" in cached_composition:
            return cached_composition

        fights = summary["fights"]
        kill_fight_ids = {f["id"] for f in fights if f["kill"]}
        wipe_fight_ids = [f["id"] for f in fights if f["id"] not in kill_fight_ids]

        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session:
            _, wipe_tally = await self._fetch_player_details(session, headers, report_code, wipe_fight_ids)

        tally = {name: dict(counts) for name, counts in summary["kill_fight_roles"].items()}
        for name, counts in wipe_tally.items():
            entry = tally.setdefault(name, {"tank": 0, "healer": 0, "dps": 0, "total": 0, "class": None})
            for key in ("tank", "healer", "dps", "total"):
                entry[key] += counts[key]
            if entry.get("class") is None:
                entry["class"] = counts.get("class")

        tanks, healers, dps, classes = [], [], [], {}
        for name, counts in tally.items():
            classes[name] = counts.get("class")
            total = counts["total"] or 1
            tank_frac = counts["tank"] / total
            healer_frac = counts["healer"] / total
            if tank_frac >= ROLE_THRESHOLD and tank_frac >= healer_frac:
                tanks.append(name)
            elif healer_frac >= ROLE_THRESHOLD:
                healers.append(name)
            else:
                dps.append(name)

        composition = {"tanks": tanks, "healers": healers, "dps": dps, "classes": classes}
        cached_entry["role_composition"] = composition
        self._report_cache.set(report_code, **cached_entry)
        return composition

    async def get_report_aura_uptime(self, report_code: str, boss_fight_ids: list) -> dict:
        """
        Best-effort raid-wide uptime for config.TRACKED_DEBUFFS (kept on the
        boss) and config.TRACKED_BUFFS (kept on a player), matched by
        ability NAME - see that config comment for why name, not spell ID.
        For each tracked ability, returns BOTH an "all fights" uptime %
        (bosses + trash, the report's whole fight list) and a "boss fights
        only" uptime % (just boss_fight_ids, supplied by the caller since
        only it knows which tier/bosses are in play for this report), plus
        whichever raider contributed the most of the BOSS-FIGHT uptime (the
        applying player for a debuff on the boss, the wearer for a buff on
        a player) - trash uptime is too inconsistent raid-to-raid to be a
        meaningful "who kept this up" callout.

        Like get_report_role_composition, this needs caller-supplied context
        (boss_fight_ids) get_report_summary() doesn't have, so it's its own
        lazily-cached call rather than folded into that always-cheap fetch -
        cached on the same per-report entry, invalidated if a later call
        passes a different boss_fight_ids (e.g. a report re-summarized under
        a different tier pick).

        This whole feature's WCL table shape - especially the "by source"/
        "by target" per-player breakdown - could not be verified against a
        live report from this sandbox (see REPORT_DEBUFFS_BY_SOURCE_QUERY's
        docstring). A shape surprise just means that ability's percentages
        and/or top-player come back None, never a crash - if numbers look
        wrong/empty once tested against a real report, this is the first
        place to check.

        Returns {ability_name: {"all_pct": float|None, "boss_pct": float|None,
                                  "top_player": str|None, "top_player_pct": float|None}}
        """
        summary = await self.get_report_summary(report_code)
        if not summary.get("fights"):
            return {}

        cache_key_ids = sorted(boss_fight_ids)
        cached_entry = self._report_cache.get(report_code) or {}
        cached = cached_entry.get("aura_uptime")
        if cached is not None and cached.get("boss_fight_ids") == cache_key_ids:
            return cached["data"]

        fights = summary["fights"]
        all_fight_ids = [f["id"] for f in fights]

        def _duration_ms(ids: set) -> int:
            return sum(
                (f["end_time"] or 0) - (f["start_time"] or 0) for f in fights
                if f["id"] in ids and f["start_time"] is not None and f["end_time"] is not None
            )

        all_duration_ms = _duration_ms(set(all_fight_ids))
        boss_duration_ms = _duration_ms(set(boss_fight_ids))

        def _pct(uptime_ms, duration_ms):
            if not duration_ms:
                return None
            return min(100.0, uptime_ms / duration_ms * 100)

        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session:
            debuffs_all = await self._fetch_aura_uptime(session, headers, report_code, all_fight_ids, REPORT_DEBUFFS_QUERY)
            debuffs_boss = await self._fetch_aura_uptime(session, headers, report_code, boss_fight_ids, REPORT_DEBUFFS_QUERY)
            debuffs_by_source = await self._fetch_aura_uptime_by_player(
                session, headers, report_code, boss_fight_ids, REPORT_DEBUFFS_BY_SOURCE_QUERY
            )
            buffs_all = await self._fetch_aura_uptime(session, headers, report_code, all_fight_ids, REPORT_BUFFS_QUERY)
            buffs_boss = await self._fetch_aura_uptime(session, headers, report_code, boss_fight_ids, REPORT_BUFFS_QUERY)
            buffs_by_target = await self._fetch_aura_uptime_by_player(
                session, headers, report_code, boss_fight_ids, REPORT_BUFFS_BY_TARGET_QUERY
            )

        result = {}
        for name, all_uptime, boss_uptime, by_player in (
            *((name, debuffs_all, debuffs_boss, debuffs_by_source) for name in config.TRACKED_DEBUFFS),
            *((name, buffs_all, buffs_boss, buffs_by_target) for name in config.TRACKED_BUFFS),
        ):
            entry = {
                "all_pct": _pct(all_uptime[name], all_duration_ms) if name in all_uptime else None,
                "boss_pct": _pct(boss_uptime[name], boss_duration_ms) if name in boss_uptime else None,
                "top_player": None, "top_player_pct": None,
            }
            per_player = by_player.get(name)
            if per_player:
                top_name, top_uptime_ms = max(per_player.items(), key=lambda kv: kv[1])
                entry["top_player"] = top_name
                entry["top_player_pct"] = _pct(top_uptime_ms, boss_duration_ms)
            result[name] = entry

        cached_entry["aura_uptime"] = {"boss_fight_ids": cache_key_ids, "data": result}
        self._report_cache.set(report_code, **cached_entry)
        return result

    async def get_report_kill_counts(self, report_code: str) -> dict:
        """
        Returns {character_name: kill_count} - how many distinct boss-kill
        fights each character appeared in, for this report. Thin wrapper
        around get_report_summary() (kept as its own method since
        attendance.py already calls this name directly) - see that method
        for the actual (cached) fetching.
        """
        summary = await self.get_report_summary(report_code)
        return summary.get("kill_counts", {})

    async def get_guild_zone_rankings(self, guild_name: str, server_slug: str, server_region: str, zone_id: int):
        """
        Best-effort guild-vs-guild speed/execution rankings across a zone
        (the raid summary's "guild rank" section) - guild-level rather than
        report-level, so NOT part of the per-report cache above, but cheap
        enough (one request) to just call fresh each time it's needed.

        Returns None (never raises) if the guild/zone can't be found or the
        response shape is unexpected - see get_report_summary's parse
        helpers for the same reasoning; this shape is likewise unverified
        from this sandbox. The field name itself (Guild.zoneRanking,
        singular) WAS confirmed live: the original guess "zoneRankings" got
        a clear WCL error naming the correct field. Its sub-selection
        (see GUILD_ZONE_RANKINGS_QUERY) was also confirmed live to be
        REQUIRED - zoneRanking is a typed GuildZoneRankings! object, not a
        JSON scalar like Character.zoneRankings - but the exact field
        names inside it are still a guess (borrowed from the confirmed
        Character.zoneRankings shape), so this can still return None on a
        shape mismatch even with no errors reported.
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        variables = {
            "name": guild_name, "serverSlug": server_slug,
            "serverRegion": server_region, "zoneId": zone_id,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    API_URL,
                    json={"query": GUILD_ZONE_RANKINGS_QUERY, "variables": variables},
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
        except Exception:
            log.warning("Failed to fetch guild zone rankings for %s", guild_name, exc_info=True)
            return None

        if "errors" in payload:
            log.warning("WCL error fetching guild zone rankings: %s", payload["errors"])
            return None

        guild = payload.get("data", {}).get("guildData", {}).get("guild")
        if not guild:
            return None

        raw = guild.get("zoneRanking")
        if not isinstance(raw, dict):
            return None

        per_boss = []
        try:
            for r in raw.get("rankings") or []:
                encounter = r.get("encounter") or {}
                per_boss.append({
                    "encounter_id": encounter.get("id"),
                    "encounter_name": encounter.get("name"),
                    "rank": r.get("rank"),
                    "server_rank": r.get("serverRank"),
                    "region_rank": r.get("regionRank"),
                    "speed_percent": r.get("rankPercent"),
                })
        except Exception:
            log.warning("Unexpected shape for guild zone rankings - skipping", exc_info=True)
            return None

        return {
            "guild_name": guild.get("name"),
            "best_performance_average": raw.get("bestPerformanceAverage"),
            "per_boss": per_boss,
        }

    async def get_report_attendance(self, report_code: str, min_kills: int = 1) -> set:
        """
        Returns the set of character names present in at least `min_kills`
        distinct boss-kill fights in this report - used for attendance
        tracking. Thin wrapper around get_report_kill_counts(), which does
        the actual (and cached) WCL fetching.
        """
        kill_counts = await self.get_report_kill_counts(report_code)
        return {name for name, count in kill_counts.items() if count >= min_kills}