"""
Minimal Wowhead item lookup client - resolves an itemID (all Gargul's loot
export gives us) into a display name, icon, and quality, so loot can be
shown with real names/icons/links instead of bare numbers.

*** NEEDS A ONE-TIME LIVE VERIFICATION PASS ***
This sandbox's network egress is locked down to an allowlist that doesn't
include wowhead.com, so the tooltip endpoint/params below could NOT be
tested against a live response while writing this file. The endpoint and
JSON shape (nether.wowhead.com/tooltip/item/<id>, keys "name"/"icon"/
"quality") are the long-standing convention used by many WoW addons/bots,
but config.WOWHEAD_DATA_ENV in particular (which dataset - retail vs a
Classic version - the tooltip is pulled from) is a best guess and MUST be
checked once the bot is actually running: post a summary, and if item names/
icons come back wrong (or as "Item #NNNNN" fallbacks) for your Fresh/TBC
realm, open a real item page on wowhead.com, check its tooltip widget's
network request in devtools, and copy the dataEnv value it actually uses
into config.py.

Nothing here ever raises - a failed/unexpected lookup just falls back to a
plain "Item #<id>" with no icon, same philosophy as icons.py.
"""

import logging
import aiohttp

import config
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.wowhead")

TOOLTIP_URL = "https://nether.wowhead.com/tooltip/item/{item_id}"

# Wowhead's own convention for "no icon" - used as a last-resort fallback so
# callers always get a usable icon URL.
FALLBACK_ICON = "inv_misc_questionmark"


def item_wowhead_url(item_id: int) -> str:
    """Wowhead's plain (non-expansion-specific) item URL - always resolves,
    unlike the versioned /tbc/, /classic/, /wotlk/ paths which would need
    the same kind of live verification called out above."""
    return f"https://www.wowhead.com/item={item_id}"


def item_icon_url(icon_slug: str | None) -> str:
    # Same wow.zamimg.com CDN convention already relied on elsewhere in this
    # repo (see config.py's CLASS_ICON_URLS etc.) - proven to work.
    return f"https://wow.zamimg.com/images/wow/icons/large/{icon_slug or FALLBACK_ICON}.jpg"


class WowheadClient:
    def __init__(self, cache_path: str = "wowhead_item_cache.json"):
        # Item name/icon/quality never changes once looked up, so this cache
        # is permanent (unlike wcl_client's report cache, nothing here ever
        # needs invalidating).
        self._cache = ApplicationStore(path=cache_path)

    async def get_item(self, item_id: int) -> dict:
        """Returns {"id", "name", "icon_slug", "icon_url", "wowhead_url", "quality"}.
        Falls back to a generic placeholder (never raises) if the lookup fails
        or comes back in an unexpected shape."""
        cached = self._cache.get(item_id)
        if cached is not None:
            return cached

        result = await self._fetch(item_id)
        self._cache.set(item_id, **result)
        return result

    async def _fetch(self, item_id: int) -> dict:
        fallback = {
            "id": item_id,
            "name": f"Item #{item_id}",
            "icon_slug": None,
            "icon_url": item_icon_url(None),
            "wowhead_url": item_wowhead_url(item_id),
            "quality": None,
        }

        params = {"locale": "0"}
        if config.WOWHEAD_DATA_ENV is not None:
            params["dataEnv"] = str(config.WOWHEAD_DATA_ENV)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    TOOLTIP_URL.format(item_id=item_id),
                    params=params,
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except Exception:
            log.warning("Wowhead lookup failed for item %s - using placeholder", item_id, exc_info=True)
            return fallback

        name = data.get("name")
        if not name:
            log.warning("Wowhead lookup for item %s returned no name - using placeholder", item_id)
            return fallback

        icon_slug = data.get("icon")
        return {
            "id": item_id,
            "name": name,
            "icon_slug": icon_slug,
            "icon_url": item_icon_url(icon_slug),
            "wowhead_url": item_wowhead_url(item_id),
            "quality": data.get("quality"),
        }
