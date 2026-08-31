"""
Minimal Blizzard Game Data API client for item lookups - resolves an
itemID (all Gargul's loot export gives us) into a display name, icon, and
quality, same contract as wowhead.py's WowheadClient.get_item(), so
cogs/raid_summary.py can use either interchangeably.

Added (2026-08) after wowhead.py's scraped XML feed turned out to be
blocked outright for this bot's hosting IP (confirmed live: AWS
CloudFront/WAF returning a static "403 - Request blocked" page on every
single request, while the identical URL worked fine from a browser on a
different network - not something any header/pacing/redirect fix could
address, since the block is on the IP, not the request shape). Blizzard's
own Game Data API is the sanctioned, first-party alternative: no scraping,
no anti-bot concerns, and it's keyed by the game's own real item ID (the
same ID Gargul exports), so there's no retail-vs-Classic ID-space mismatch
to get wrong either (see wowhead.py's ITEM_XML_URL comment for that
history).

Docs: https://develop.battle.net/documentation/world-of-warcraft/game-data-apis
(this sandbox can never reach develop.battle.net or oauth.battle.net /
*.api.blizzard.com to verify any of this live - built from Blizzard's
documented OAuth client-credentials flow plus a real, working third-party
client library's source (lostcol0ny/blizzardapi2 on GitHub) for the exact
URL/namespace shapes, since Blizzard's own docs site is unreachable from
here too. The one thing that needs confirming against a REAL response is
the exact JSON field names below - see get_item()'s docstring and
raidsummary_test_blizzard_api for how to check that once real credentials
exist.)

- OAuth token URL: https://oauth.battle.net/oauth/token (client_credentials
  grant, HTTP Basic auth with client_id/client_secret - same flow
  wcl_client.py already uses for WarcraftLogs, just Blizzard's own
  endpoint/param shape, confirmed against blizzardapi2's source: grant_type
  sent as a query param, not a form body).
- API base: https://{region}.api.blizzard.com (region: us/eu/kr/tw/cn -
  same SERVER_REGION env var wcl_client.py already reads).
- Item: GET /data/wow/item/{item_id}?namespace=static-classic-{region}&locale=en_US
- Item media (icon): GET /data/wow/media/item/{item_id}?namespace=static-classic-{region}&locale=en_US
  (namespace "static-classic-{region}" is Blizzard's namespace for
  Classic PROGRESSION realms - i.e. TBC/Cataclysm Classic, confirmed via a
  Blizzard forum thread on TBC Anniversary/"Fresh" realms specifically
  moving from "classic1x-{region}" (Classic Era) to this namespace -
  "static-classic1x-{region}" is Classic ERA instead, for a guild still on
  original/Era Classic rather than TBC+).

Nothing here ever raises - a failed/unexpected lookup just falls back to a
plain "Item #<id>" with no icon, same philosophy as wowhead.py/icons.py.
Only ever used if BLIZZARD_CLIENT_ID/BLIZZARD_CLIENT_SECRET are configured
(see bot.py) - cogs/raid_summary.py falls back to wowhead.WowheadClient
automatically otherwise, so this being unset just means the bot behaves
exactly as it did before this file existed.
"""

import time
import logging
import aiohttp

from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.blizzard")

OAUTH_URL = "https://oauth.battle.net/oauth/token"


def _api_base(region: str) -> str:
    return f"https://{region}.api.blizzard.com"


def _namespace(region: str) -> str:
    return f"static-classic-{region}"


class BlizzardClient:
    def __init__(self, client_id: str, client_secret: str, region: str = "us",
                 cache_path: str = "blizzard_item_cache.json"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.region = region
        self._token = None
        self._token_expires_at = 0
        # Same permanent-cache-on-success/retry-on-failure contract as
        # wowhead.py's WowheadClient - see that file's get_item() docstring
        # for why a partial/failed result must never be cached as if it
        # were a real one.
        self._cache = ApplicationStore(path=cache_path)

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        auth_header = aiohttp.BasicAuth(self.client_id, self.client_secret).encode()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OAUTH_URL,
                params={"grant_type": "client_credentials"},
                headers={"Authorization": auth_header},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 86400)
        return self._token

    async def get_item(self, item_id: int) -> dict:
        """Returns {"id", "name", "icon_slug", "icon_url", "quality"} -
        NOT "wowhead_url" (unlike wowhead.WowheadClient.get_item()) since
        that's a Wowhead-specific display link this client has no opinion
        on; callers that want one should build it themselves via
        wowhead.item_wowhead_url(item_id) regardless of which client
        resolved the item's name/icon/quality. `quality` is an int 0-5
        matching config.ITEM_QUALITY_COLORS' keys - see _fetch's docstring
        for the string->int mapping, since Blizzard's API returns a named
        quality type (e.g. "EPIC"), not Wowhead's numeric id.

        Same cache-worthiness contract as wowhead.py's get_item(): a
        result is only cached if it fully resolved (icon_slug present AND
        name isn't the "Item #<id>" placeholder) - see _is_resolved.
        """
        cached = self._cache.get(item_id)
        if cached is not None and _is_resolved(cached, item_id):
            return cached

        result = await self._fetch(item_id)
        if _is_resolved(result, item_id):
            self._cache.set(item_id, **result)
        return result

    async def _fetch(self, item_id: int) -> dict:
        fallback = {"id": item_id, "name": f"Item #{item_id}", "icon_slug": None, "icon_url": None, "quality": None}

        try:
            token = await self._get_token()
        except Exception:
            log.warning("Couldn't get a Blizzard API token - using placeholder for item %s", item_id, exc_info=True)
            return fallback

        headers = {"Authorization": f"Bearer {token}"}
        base = _api_base(self.region)
        namespace = _namespace(self.region)
        params = {"namespace": namespace, "locale": "en_US"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/data/wow/item/{item_id}", params=params, headers=headers) as resp:
                    if resp.status == 404:
                        log.warning("Blizzard API has no item %s under namespace %s - using placeholder", item_id, namespace)
                        return fallback
                    resp.raise_for_status()
                    item_data = await resp.json()

                async with session.get(f"{base}/data/wow/media/item/{item_id}", params=params, headers=headers) as resp:
                    resp.raise_for_status()
                    media_data = await resp.json()
        except Exception:
            log.warning(
                "Blizzard API lookup failed for item %s (namespace %s) - using placeholder",
                item_id, namespace, exc_info=True,
            )
            return fallback

        # Field names below are the DOCUMENTED Game Data API item/item-media
        # response shape - not independently re-verified against a live
        # response (see the module docstring: this sandbox can't reach
        # api.blizzard.com either). If names/icons come back wrong or
        # missing once real credentials exist, dump the raw item_data/
        # media_data here first (e.g. via raidsummary_test_blizzard_api)
        # rather than guessing at another field name blind.
        name = item_data.get("name") or f"Item #{item_id}"
        quality_type = ((item_data.get("quality") or {}).get("type") or "").upper()
        quality = _QUALITY_TYPE_TO_INT.get(quality_type)

        icon_url = None
        icon_slug = None
        for asset in media_data.get("assets") or []:
            if asset.get("key") == "icon" and asset.get("value"):
                icon_url = asset["value"]
                icon_slug = icon_url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                break

        return {"id": item_id, "name": name, "icon_slug": icon_slug, "icon_url": icon_url, "quality": quality}

    async def diagnose_item(self, item_id: int) -> str:
        """
        Step-by-step verbose report for /raidsummary-test-blizzard - unlike
        get_item()/_fetch() (which fall back to a placeholder on ANY
        failure, by design, so a raid summary never breaks over one bad
        item), this surfaces exactly which step failed and the raw
        response bodies, since NOTHING in this file has been verified
        against a real Blizzard response yet (see the module docstring).
        Never raises - any exception is caught and reported as a step
        result instead, same "always returns something useful" contract
        as everything else here.
        """
        lines = [f"**Diagnosing item {item_id}** (region={self.region}, namespace={_namespace(self.region)})"]

        try:
            token = await self._get_token()
            lines.append(f"✅ OAuth token acquired ({token[:8]}...)")
        except Exception as e:
            lines.append(f"❌ OAuth token request failed: {e!r}")
            return "\n".join(lines)

        headers = {"Authorization": f"Bearer {token}"}
        base = _api_base(self.region)
        params = {"namespace": _namespace(self.region), "locale": "en_US"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/data/wow/item/{item_id}", params=params, headers=headers) as resp:
                    item_body = await resp.text()
                    lines.append(f"Item endpoint: HTTP {resp.status}\n```\n{item_body[:1500]}\n```")

                async with session.get(f"{base}/data/wow/media/item/{item_id}", params=params, headers=headers) as resp:
                    media_body = await resp.text()
                    lines.append(f"Item media endpoint: HTTP {resp.status}\n```\n{media_body[:1500]}\n```")
        except Exception as e:
            lines.append(f"❌ Request failed: {e!r}")
            return "\n".join(lines)

        try:
            result = await self._fetch(item_id)
            lines.append(f"Parsed result: `{result}`")
        except Exception as e:
            lines.append(f"❌ Parsing raised (this shouldn't happen - _fetch never raises normally): {e!r}")

        return "\n".join(lines)


# Blizzard's quality.type strings -> config.ITEM_QUALITY_COLORS' int keys
# (0=poor..5=legendary) - TBC Classic never has artifact(6)/heirloom(7).
_QUALITY_TYPE_TO_INT = {
    "POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3, "EPIC": 4, "LEGENDARY": 5,
}


def _is_resolved(result: dict, item_id: int) -> bool:
    return result.get("icon_slug") is not None and result.get("name") != f"Item #{item_id}"
