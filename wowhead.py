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

Caveat: this still wasn't independently re-verified against a live request
while fixing this (this sandbox can't reach wowhead.com either).

History of live symptoms so far (2026-08):
  1. EVERY loot item was coming back as "Item #<id>" - a browser User-Agent
     (REQUEST_HEADERS) fixed that.
  2. After that, a raid-summary regenerate with several tracked buffs/
     debuffs (config.TRACKED_ABILITY_ICON_SPELL_IDS) started getting a hard
     403 Forbidden on EVERY spell lookup in that run, all in the same
     traceback. The giveaway isn't the User-Agent this time - it's that
     _build_uptime_lines calls get_spell() for each tracked ability back to
     back with ZERO delay between requests (unlike _build_loot_lines /
     icons.ensure_item_emoji, which already paces ITS calls at 0.3s - but
     only around emoji creation, never between the wowhead fetches
     themselves), and a raid summary can also fire a burst of item lookups
     moments earlier for a big loot night. A burst of rapid same-origin
     requests with no pacing at all is a textbook trigger for Cloudflare
     (or similar) rate-limit/bot-management blocking - hence PACE_SECONDS
     below, enforced once here so every current and future caller is
     covered without having to remember to add sleeps at each call site.

If placeholders/403s keep showing up after both of these, that's the next
thing to check (Wowhead tightening the challenge further - a JS/cookie
challenge no static header set or pacing can clear - at which point the
XML feed may need replacing with Wowhead's tooltip JSON endpoint, or an
official Blizzard Game Data API item lookup, instead).

Nothing here ever raises - a failed/unexpected lookup just falls back to a
plain "Item #<id>" with no icon, same philosophy as icons.py.
"""

import re
import time
import asyncio
import logging
import aiohttp

from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.wowhead")

ITEM_XML_URL = "https://www.wowhead.com/item={item_id}&xml"
SPELL_XML_URL = "https://www.wowhead.com/spell={spell_id}&xml"

# Likely cause of the "every item comes back as Item #<id>" symptom (see
# the module docstring): aiohttp's default User-Agent ("Python/3.x
# aiohttp/3.x") is a common trigger for an anti-bot challenge page instead
# of the real XML feed (no <icon>/<name> tags in it). A real browser UA is
# the standard fix for that class of block - same fix already needed
# nowhere else in this repo since every other outbound call here is either
# WarcraftLogs' own API (its own client library conventions apply, not a
# browser's) or the wow.zamimg.com icon CDN (no anti-bot gate at all).
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.wowhead.com/",
}

# Minimum gap enforced between ANY two requests this client makes to
# wowhead.com (see the module docstring's #2) - a raid summary can fire a
# couple dozen of these (every unique loot item + every tracked buff/debuff
# icon) in a single run with nothing else to naturally space them out.
PACE_SECONDS = 0.5

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


def spell_wowhead_url(spell_id: int) -> str:
    """Wowhead's plain (non-expansion-specific) spell URL - see item_wowhead_url."""
    return f"https://www.wowhead.com/spell={spell_id}"


class WowheadClient:
    def __init__(self, cache_path: str = "wowhead_item_cache.json"):
        # Item name/icon/quality never changes once looked up, so a
        # SUCCESSFUL lookup is cached permanently (unlike wcl_client's
        # report cache, a real result here never needs invalidating). A
        # FAILED lookup (icon_slug None - see _fetch's fallback) is
        # deliberately never written here - see get_item()'s docstring for
        # why a fallback used to get cached just as permanently as a real
        # result, which is exactly backwards.
        self._cache = ApplicationStore(path=cache_path)
        # Serializes + paces every outbound wowhead.com request (item and
        # spell alike) - see PACE_SECONDS/the module docstring. A lock, not
        # just a "last request time" timestamp, so two lookups awaited
        # concurrently (e.g. via asyncio.gather elsewhere) can't both read
        # the same "long enough ago" timestamp and fire back-to-back anyway.
        self._pace_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def _pace(self):
        async with self._pace_lock:
            wait = self._last_request_at + PACE_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def get_item(self, item_id: int) -> dict:
        """Returns {"id", "name", "icon_slug", "icon_url", "wowhead_url", "quality"}.
        Falls back to a generic placeholder (never raises) if the lookup fails
        or comes back in an unexpected shape.

        A cached entry with icon_slug=None is treated as a cache MISS, not a
        hit - it can only mean a past lookup failed (a successful one always
        has an icon_slug), and this used to get cached exactly like a real
        result, permanently freezing every "Item #<id>" placeholder forever
        even after whatever caused the failure (e.g. the anti-bot-challenge
        User-Agent issue - see the module docstring) got fixed, since
        nothing ever re-tried it. Retrying here on every call this comes up
        costs nothing once a real result lands (see _fetch), since that's
        the one case that DOES get cached and short-circuits every call
        after."""
        cached = self._cache.get(item_id)
        if cached is not None and cached.get("icon_slug") is not None:
            return cached

        result = await self._fetch(item_id)
        if result.get("icon_slug") is not None:
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
            await self._pace()
            async with aiohttp.ClientSession() as session:
                async with session.get(ITEM_XML_URL.format(item_id=item_id), headers=REQUEST_HEADERS) as resp:
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

    async def get_spell(self, spell_id: int) -> dict:
        """Returns {"id", "name", "icon_slug", "icon_url", "wowhead_url"} for
        a spell - same permanent-cache-on-success/retry-on-failure contract
        as get_item() (see its docstring), used to give
        cogs/raid_summary.py's buff/debuff-uptime section a real icon per
        tracked ability (config.TRACKED_ABILITY_ICON_SPELL_IDS). Cached
        under a "spell:<id>" string key in the same store as items (plain
        int item IDs), so the two never collide."""
        cache_key = f"spell:{spell_id}"
        cached = self._cache.get(cache_key)
        if cached is not None and cached.get("icon_slug") is not None:
            return cached

        result = await self._fetch_spell(spell_id)
        if result.get("icon_slug") is not None:
            self._cache.set(cache_key, **result)
        return result

    async def _fetch_spell(self, spell_id: int) -> dict:
        fallback = {
            "id": spell_id,
            "name": f"Spell #{spell_id}",
            "icon_slug": None,
            "icon_url": item_icon_url(None),
            "wowhead_url": spell_wowhead_url(spell_id),
        }

        try:
            await self._pace()
            async with aiohttp.ClientSession() as session:
                async with session.get(SPELL_XML_URL.format(spell_id=spell_id), headers=REQUEST_HEADERS) as resp:
                    resp.raise_for_status()
                    text = await resp.text()
        except Exception:
            log.warning("Wowhead lookup failed for spell %s - using placeholder", spell_id, exc_info=True)
            return fallback

        icon_match = ICON_TAG_RE.search(text)
        if not icon_match:
            log.warning("Wowhead lookup for spell %s returned no <icon> tag - using placeholder", spell_id)
            return fallback

        name_match = NAME_TAG_RE.search(text)
        icon_slug = icon_match.group(1).strip()

        return {
            "id": spell_id,
            "name": name_match.group(1).strip() if name_match else f"Spell #{spell_id}",
            "icon_slug": icon_slug,
            "icon_url": item_icon_url(icon_slug),
            "wowhead_url": spell_wowhead_url(spell_id),
        }
