"""
Blizzard Battle.net API client: the app's own token (client credentials,
cached until it expires) and requests to any game data or profile path
(`request`), plus item and spell icon lookups used by raid summaries and
the emoji library (`get_item`, `get_spell_icon`, cached permanently on
success).

Game-version specifics (namespaces, locale, what each game offers) live in
wow_api.py and the settings, not here; docs/wow-forever-api.md describes
what Blizzard's API answers for WoW Forever.

  - OAuth token: https://oauth.battle.net/oauth/token (client_credentials,
    HTTP Basic auth with the client id and secret)
  - API base: https://{region}.api.blizzard.com
  - Item / item media: /data/wow/item/{id}, /data/wow/media/item/{id} in a
    static namespace (e.g. static-forever-eu)

A failed item lookup falls back to a plain "Item #<id>" with no icon and
is retried next time; nothing here raises for a missing item.
"""

import time
import logging
import aiohttp
from typing import Optional

from http_utils import basic_auth_header
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.blizzard")

OAUTH_URL = "https://oauth.battle.net/oauth/token"


def _api_base(region: str) -> str:
    return f"https://{region}.api.blizzard.com"


def _namespace(region: str) -> str:
    return f"static-classic-{region}"


def _parse_icon_asset(media_data: dict) -> tuple:
    """Returns (icon_url, icon_slug) from a /data/wow/media/item or
    /data/wow/media/spell response's "assets" list - shared by get_item()
    and get_spell_icon(), which both hit a media endpoint of this same
    shape (confirmed live for items - see get_item()'s docstring; not
    independently confirmed for spells, but documented as the same
    "assets": [{"key": "icon", "value": <url>}, ...] shape). (None, None)
    if no "icon" asset is present."""
    for asset in media_data.get("assets") or []:
        if asset.get("key") == "icon" and asset.get("value"):
            icon_url = asset["value"]
            icon_slug = icon_url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            return icon_url, icon_slug
    return None, None


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
            log.debug("Reusing cached Blizzard API token (expires in %.0fs)", self._token_expires_at - time.time())
            return self._token

        auth_header = basic_auth_header(self.client_id, self.client_secret)
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                OAUTH_URL,
                params={"grant_type": "client_credentials"},
                headers={"Authorization": auth_header},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 86400)
        log.debug("Fetched a new Blizzard API token (expires in %ss)", data.get("expires_in", 86400))
        return self._token

    async def request(self, path: str, namespace: str, params: Optional[dict] = None, *,
                      session: Optional[aiohttp.ClientSession] = None, region: Optional[str] = None,
                      token: Optional[str] = None, locale: str = "en_US") -> tuple:
        """GET any API path (e.g. "/data/wow/realm/index") with a full
        namespace ("dynamic-forever-eu"). Returns (status, headers, json or
        None). Uses the app's own token unless a player's `token` is given
        (e.g. for /profile/user/wow). Raises only for network failures."""
        token = token or await self._get_token()
        query = {"namespace": namespace, "locale": locale, **(params or {})}
        if session is None:
            async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=15)) as own:
                return await self.request(path, namespace, params, session=own, region=region, token=token,
                                          locale=locale)
        async with session.get(f"{_api_base(region or self.region)}{path}", params=query,
                               headers={"Authorization": f"Bearer {token}"}) as resp:
            body = await resp.json(content_type=None) if resp.status == 200 else None
            return resp.status, dict(resp.headers), body

    async def get_game_data(self, path: str, namespace: str, params: Optional[dict] = None,
                            session: Optional[aiohttp.ClientSession] = None) -> Optional[dict]:
        """GET /data/wow/{path} in the given namespace family ("static",
        "static-classic", ...; the region is appended). Returns the JSON, or
        None when Blizzard has no such record (404). Other failures raise,
        so callers can tell "doesn't exist" from "couldn't ask". Pass a
        `session` to reuse one connection for many requests."""
        status, _headers, body = await self.request(f"/data/wow/{path}", f"{namespace}-{self.region}", params,
                                                    session=session)
        if status == 404:
            return None
        if status != 200:
            raise aiohttp.ClientResponseError(None, (), status=status, message=f"Blizzard API answered {status}")
        return body

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
            log.debug("Item %s: cache hit, skipping Blizzard API fetch", item_id)
            return cached
        log.debug("Item %s: cache miss (or unresolved entry) - fetching from Blizzard API", item_id)

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
            async with aiohttp.ClientSession(trust_env=True) as session:
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

        # Game Data API item/item-media fields (checked live). If names or
        # icons look wrong for a game version, compare with the raw answer
        # in the Blizzard API explorer (Maintenance) first.
        name = item_data.get("name") or f"Item #{item_id}"
        quality_type = ((item_data.get("quality") or {}).get("type") or "").upper()
        quality = _QUALITY_TYPE_TO_INT.get(quality_type)
        icon_url, icon_slug = _parse_icon_asset(media_data)

        return {"id": item_id, "name": name, "icon_slug": icon_slug, "icon_url": icon_url, "quality": quality}

    async def get_spell_icon(self, spell_id: int) -> dict:
        """
        Returns {"icon_slug", "icon_url"} for a spell/ability's icon via
        Blizzard's media-only spell endpoint (/data/wow/media/spell/{id} -
        confirmed to exist via a Blizzard forum thread on spell icons, not
        independently verified live here either - see the module
        docstring). Deliberately does NOT return "name" the way get_item()
        does - callers here (cogs/raid_summary.py's tracked buff/debuff
        uptime section) already know the ability's real name from
        config.TRACKED_DEBUFFS/TRACKED_BUFFS (that's what's used to match
        it against WCL data in the first place - see wcl_client.
        get_report_aura_uptime), so there's nothing to gain by asking
        Blizzard for a name here, unlike items where Gargul's export gives
        us only a bare ID and no name at all.

        Blizzard's media endpoint can return HTTP 200 with a real icon
        asset for a spell_id that isn't a meaningful match under the
        Classic namespace - the asset it hands back is
        "inv_misc_questionmark", WoW's own real in-client placeholder icon
        for "no icon assigned", not a 404. This is treated as NOT resolved
        (see _PLACEHOLDER_SPELL_ICON_SLUGS below) so it's never cached and
        never reported as a found icon - callers (cogs/raid_summary.py's
        _get_spell_icon_url) fall through to Wowhead instead, same as a
        real 404, rather than trusting any non-None icon_slug and silently
        showing this generic "?" icon. A couple of TRACKED_ABILITY_ICON_
        SPELL_IDS entries (the Judgements) still need a manual
        TRACKED_ABILITY_ICON_EMOJI override that bypasses this method
        entirely, but this check guards every OTHER spell_id lookup.

        Same cache-worthiness contract as get_item() - a result is only
        cached once it has a real (non-placeholder) icon_slug, so a
        failed/not-yet-resolved lookup retries on every call instead of
        freezing "no icon" forever.
        Cached under a "spell:<id>" string key in the same store as items
        (plain int item IDs), so the two never collide - same convention
        wowhead.py's cache already uses.
        """
        cache_key = f"spell:{spell_id}"
        cached = self._cache.get(cache_key)
        if cached is not None and cached.get("icon_slug") is not None:
            return cached

        fallback = {"icon_slug": None, "icon_url": None}
        try:
            token = await self._get_token()
        except Exception:
            log.warning("Couldn't get a Blizzard API token - no icon for spell %s", spell_id, exc_info=True)
            return fallback

        headers = {"Authorization": f"Bearer {token}"}
        base = _api_base(self.region)
        params = {"namespace": _namespace(self.region), "locale": "en_US"}

        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(f"{base}/data/wow/media/spell/{spell_id}", params=params, headers=headers) as resp:
                    if resp.status == 404:
                        log.warning("Blizzard API has no media for spell %s - no icon", spell_id)
                        return fallback
                    resp.raise_for_status()
                    media_data = await resp.json()
        except Exception:
            log.warning("Blizzard API lookup failed for spell %s - no icon", spell_id, exc_info=True)
            return fallback

        icon_url, icon_slug = _parse_icon_asset(media_data)
        if icon_slug in _PLACEHOLDER_SPELL_ICON_SLUGS:
            log.info(
                "Blizzard returned its generic placeholder icon (%s) for spell %s under namespace %s - "
                "treating as unresolved so callers fall back to Wowhead instead",
                icon_slug, spell_id, _namespace(self.region),
            )
            icon_url, icon_slug = None, None

        result = {"icon_slug": icon_slug, "icon_url": icon_url}
        if icon_slug is not None:
            self._cache.set(cache_key, **result)
        return result

def _is_resolved(result: dict, item_id: int) -> bool:
    return result.get("icon_slug") is not None and result.get("name") != f"Item #{item_id}"
