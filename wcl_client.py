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

from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.wcl")

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://fresh.warcraftlogs.com/api/v2/client"

# A character counts as Tank/Healer for get_report_role_composition() only
# if they filled that role in at least this fraction of the fights they
# appeared in (handles hybrids who tank/heal some pulls and DPS others) -
# otherwise they're counted as DPS.
ROLE_THRESHOLD = 0.70

_ROLE_KEY_MAP = {"tanks": "tank", "healers": "healer", "dps": "dps"}

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
}


def _normalize_role(role_key: str) -> str:
    return _ROLE_KEY_MAP.get(role_key, "dps")

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

# Guild-level (not report-level) - a guild's speed/execution rankings across
# every boss in a zone, i.e. "how does our kill of Boss X compare to other
# guilds on the server/region". Best-effort: this JSON shape could not be
# verified live (see wcl_client's module docstring), so get_guild_zone_rankings()
# below parses it defensively and returns None on anything unexpected rather
# than raising.
GUILD_ZONE_RANKINGS_QUERY = """
query GuildZoneRankings($name: String!, $serverSlug: String!, $serverRegion: String!, $zoneId: Int!) {
  guildData {
    guild(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      name
      zoneRankings(zoneId: $zoneId)
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
                    kill_counts[name] = kill_counts.get(name, 0) + 1
                    tally = role_tally.setdefault(
                        name, {"tank": 0, "healer": 0, "dps": 0, "total": 0, "class": player.get("type")}
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
                    "damage_done": {}, "healing_done": {},
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
        helpers for the same reasoning; this JSON shape is likewise
        unverified from this sandbox.
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

        raw = guild.get("zoneRankings")
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