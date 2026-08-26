"""
Minimal Wowhead item lookup client - resolves an itemID (all Gargul's loot
export gives us) into a display name, icon, and quality, so loot can be
shown with real names/icons/links instead of bare numbers.

Uses Wowhead's `&xml` data feed (e.g. wowhead.com/item=12345&xml) - the
same mechanism cogs/emoji_admin.py's /add-emoji command already uses to
resolve an item/spell link into an icon (see that file's docstring: this
isn't scraping the regular page - its og:image meta tag is a user-submitted
screenshot, not the icon - it's a long-standing data endpoint many
third-party WoW tools rely on for exactly this). This file is now the one
place that talks to Wowhead for item data; /add-emoji still has its own
separate resolution for spell links (which this client doesn't handle) and
its own emoji-creation step, but its item-link path could be pointed at
get_item() below instead of re-fetching independently.

Caveat: this wasn't independently re-verified against a live request while
building this (this sandbox can't reach wowhead.com) - only confirmed via
documented third-party usage, same caveat /add-emoji already carries. If
item lookups keep coming back as "Item #NNNNN" placeholders, that's the
first thing to test with a single known item ID.

Nothing here ever raises - a failed/unexpected lookup just falls back to a
plain "Item #<id>" with no icon, same philosophy as icons.py.
"""

import re
import logging
import aiohttp

from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.wowhead")

ITEM_XML_URL = "https://www.wowhead.com/item={item_id}&xml"
ICON_TAG_RE = re.compile(r"<icon[^>]*>([^<]+)</icon>", re.IGNORECASE)
NAME_TAG_RE = re.compile(r"<name[^>]*>([^<]+)</name>", re.IGNORECASE)
QUALITY_TAG_RE = re.compile(r'<quality id="(\d+)"', re.IGNORECASE)

# Wowhead's own convention for "no icon" - used as a last-resort fallback so
# callers always get a usable icon URL.
FALLBACK_ICON = "inv_misc_questionmark"


def item_wowhead_url(item_id: int) -> str:
    """Wowhead's plain (non-expansion-specific) item URL - always resolves,
    unlike the versioned /tbc/, /classic/, /wotlk/ paths which would need
    their own verification."""
    return f"https://www.wowhead.com/item={item_id}"


def item_icon_url(icon_slug: str | None) -> str:
    # Same wow.zamimg.com CDN convention already relied on elsewhere in this
    # repo (see config.py's CLASS_ICON_URLS and emoji_admin.py) - proven to work.
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

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ITEM_XML_URL.format(item_id=item_id)) as resp:
                    resp.raise_for_status()
                    text = await resp.text()
        except Exception:
            log.warning("Wowhead lookup failed for item %s - using placeholder", item_id, exc_info=True)
            return fallback

        icon_match = ICON_TAG_RE.search(text)
        if not icon_match:
            log.warning("Wowhead lookup for item %s returned no <icon> tag - using placeholder", item_id)
            return fallback

        name_match = NAME_TAG_RE.search(text)
        quality_match = QUALITY_TAG_RE.search(text)
        icon_slug = icon_match.group(1).strip()

        return {
            "id": item_id,
            "name": name_match.group(1).strip() if name_match else f"Item #{item_id}",
            "icon_slug": icon_slug,
            "icon_url": item_icon_url(icon_slug),
            "wowhead_url": item_wowhead_url(item_id),
            "quality": int(quality_match.group(1)) if quality_match else None,
        }
