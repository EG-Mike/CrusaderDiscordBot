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

    async def _fetch_player_details(self, session, headers, report_code: str, kill_fight_ids: list):
        """
        Shared by get_report_summary() below - one WCL request per kill
        fight (the expensive part), returning both the per-character kill
        tally (attendance) and a best-effort name->{class, role} roster
        (raid-summary comp breakdown), from the same playerDetails response
        so this loop only ever runs once per report.

        Verified against a real report (2026-07): report.fights[].kill is
        true/false/null as expected (null = trash, false = wipe, true =
        kill), and playerDetails(fightIDs: [...]) reliably returns `name`
        per player across the dps/healers/tanks buckets. The class hint
        (player.get("type")) is best-effort and not separately verified.
        """
        kill_counts = {}
        roster = {}
        for fight_id in kill_fight_ids:
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
            for role_key, role_list in buckets.items():
                if not isinstance(role_list, list):
                    continue
                for player in role_list:
                    name = player.get("name")
                    if not name:
                        continue
                    kill_counts[name] = kill_counts.get(name, 0) + 1
                    if name not in roster:
                        roster[name] = {"class": player.get("type"), "role": role_key}

        return kill_counts, roster

    def _parse_rankings(self, raw) -> list:
        """
        Best-effort parse of Report.rankings' JSON blob into a flat list of
        {name, class, spec, rank_percent, fight_id, role}. This JSON shape
        could not be verified against a live report from this sandbox (see
        the module docstring) - on any structural surprise this logs a
        warning and returns [] rather than raising, so an unexpected WCL
        response degrades the summary's parse-highlight section to "no data"
        instead of breaking the whole report fetch.
        """
        if not isinstance(raw, dict):
            return []
        parses = []
        try:
            for entry in raw.get("data") or []:
                fight_id = entry.get("fight")
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
                            "role": role_name,
                        })
        except Exception:
            log.warning("Unexpected shape for report rankings - skipping parse highlights", exc_info=True)
            return []
        return parses

    async def _fetch_deaths(self, session, headers, report_code: str, fight_ids: list) -> dict:
        """Best-effort {character_name: death_count} across the whole
        report, one request total (not per-fight). Returns {} on any
        failure/unexpected shape - see _parse_rankings' docstring, same
        reasoning applies here."""
        if not fight_ids:
            return {}
        try:
            async with session.post(
                API_URL,
                json={
                    "query": REPORT_DEATHS_QUERY,
                    "variables": {"code": report_code, "fightIDs": fight_ids},
                },
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except Exception:
            log.warning("Failed to fetch deaths table for report %s", report_code, exc_info=True)
            return {}

        if "errors" in payload:
            log.warning("WCL error fetching deaths table for report %s: %s", report_code, payload["errors"])
            return {}

        report_data = payload.get("data", {}).get("reportData", {}).get("report") or {}
        table = report_data.get("table") or {}
        inner = table.get("data") if isinstance(table.get("data"), dict) else table
        entries = (inner or {}).get("entries") or []

        counts = Counter()
        try:
            for entry in entries:
                name = entry.get("name")
                if not name:
                    continue
                counts[name] += entry.get("total") or entry.get("count") or 1
        except Exception:
            log.warning("Unexpected shape for deaths table - skipping", exc_info=True)
            return {}
        return dict(counts)

    async def get_report_summary(self, report_code: str) -> dict:
        """
        THE single per-report fetch point. Everything any feature needs from
        a WCL report - fight list (incl. wipes/trash, not just kills),
        parse rankings, deaths, roster/comp, and the attendance kill-count
        tally - is fetched here ONCE per report code and cached together, so
        e.g. /checkattendance and a raid summary generated from the same log
        never pay for the same WCL requests twice. Cached in the same
        self._report_cache as before (see __init__'s note on it) - old
        cache entries from before this method existed just get transparently
        re-fetched once (they won't have a "fights" key yet).

        Returns:
          {
            "zone": {"id", "name"} or None,
            "start_time": ms epoch or None, "end_time": ms epoch or None,
            "fights": [{"id","name","encounter_id","kill","difficulty",
                        "start_time","end_time","boss_percentage","fight_percentage"}, ...],
            "kill_counts": {name: kill_fight_count},        # attendance - verified shape
            "roster": {name: {"class", "role"}},             # best-effort
            "parses": [{"name","class","spec","rank_percent","fight_id","role"}, ...],  # best-effort
            "deaths": {name: death_count},                   # best-effort
          }
        """
        cached = self._report_cache.get(report_code)
        if cached is not None and "fights" in cached:
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
                    "kill_counts": {}, "roster": {}, "parses": [], "deaths": {},
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

            kill_counts, roster = await self._fetch_player_details(session, headers, report_code, kill_fight_ids)
            parses = self._parse_rankings(report.get("rankings"))
            deaths = await self._fetch_deaths(session, headers, report_code, all_fight_ids)

            zone = report.get("zone")
            summary = {
                "zone": {"id": zone.get("id"), "name": zone.get("name")} if zone else None,
                "start_time": report.get("startTime"),
                "end_time": report.get("endTime"),
                "fights": fights,
                "kill_counts": kill_counts,
                "roster": roster,
                "parses": parses,
                "deaths": deaths,
            }

        self._report_cache.set(report_code, **summary)
        return summary

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