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

REPORT_FIGHTS_QUERY = """
query ReportFights($code: String!) {
  reportData {
    report(code: $code) {
      fights { id kill }
    }
  }
}
"""

REPORT_PLAYER_DETAILS_QUERY = """
query ReportPlayers($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      playerDetails(fightIDs: $fightIDs)
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

    async def get_report_kill_counts(self, report_code: str) -> dict:
        """
        Returns {character_name: kill_count} - how many distinct boss-kill
        fights each character appeared in, for this report. This is the
        expensive part (one WCL request per kill fight), so it's cached
        locally by report code once computed - see the note on
        self._report_cache in __init__ for why that's safe to do.

        Verified against a real report (2026-07): report.fights[].kill is
        true/false/null as expected (null = trash, false = wipe, true =
        kill), and playerDetails(fightIDs: [...]) reliably returns `name`
        per player across the dps/healers/tanks buckets.
        """
        cached = self._report_cache.get(report_code)
        if cached is not None:
            return cached.get("kill_counts", {})

        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                json={"query": REPORT_FIGHTS_QUERY, "variables": {"code": report_code}},
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

            if "errors" in payload:
                raise RuntimeError(f"WarcraftLogs API error: {payload['errors']}")

            report = payload.get("data", {}).get("reportData", {}).get("report")
            if not report:
                return {}

            kill_fight_ids = [f["id"] for f in (report.get("fights") or []) if f.get("kill")]
            if not kill_fight_ids:
                self._report_cache.set(report_code, kill_counts={})
                return {}

            kill_counts = {}
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
                for role_list in buckets.values():
                    if not isinstance(role_list, list):
                        continue
                    for player in role_list:
                        name = player.get("name")
                        if name:
                            kill_counts[name] = kill_counts.get(name, 0) + 1

        self._report_cache.set(report_code, kill_counts=kill_counts)
        return kill_counts

    async def get_report_attendance(self, report_code: str, min_kills: int = 1) -> set:
        """
        Returns the set of character names present in at least `min_kills`
        distinct boss-kill fights in this report - used for attendance
        tracking. Thin wrapper around get_report_kill_counts(), which does
        the actual (and cached) WCL fetching.
        """
        kill_counts = await self.get_report_kill_counts(report_code)
        return {name for name, count in kill_counts.items() if count >= min_kills}