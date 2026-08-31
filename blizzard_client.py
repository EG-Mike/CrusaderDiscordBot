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
- Character equipment: GET /profile/wow/character/{realmSlug}/{characterName}/equipment
  ?namespace=profile-classic-{region}&locale=en_US (see
  get_character_equipment() - reasonably confirmed shape, a documented
  Profile API endpoint).
- Character specializations/talents: GET /profile/wow/character/{realmSlug}/
  {characterName}/specializations?namespace=profile-classic-{region}&locale=en_US
  (see get_character_specializations() - the endpoint NAME is confirmed
  (via a real third-party client library's source), but the exact JSON
  shape for Classic's freeform talent trees is a best-effort guess, not
  independently verified - see that method's docstring for the honest
  caveat and how to check/fix it against a real response via
  diagnose_character()).

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


def _profile_namespace(region: str) -> str:
    """Namespace for character PROFILE data (equipment, specializations) -
    separate from _namespace()'s static game-data namespace above. Same
    "-classic-" progression-realm segment as static-classic-{region}, just
    under the "profile" family instead of "static" (documented Blizzard
    convention: every namespace family - static/dynamic/profile - gets its
    own namespace string for the same realm type)."""
    return f"profile-classic-{region}"


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

        CONFIRMED live (2026-08, moderator report + diagnose_spell()):
        Blizzard's media endpoint can return HTTP 200 with a real icon
        asset for a spell_id that isn't a meaningful match under the
        Classic namespace - the asset it hands back is
        "inv_misc_questionmark", WoW's own real in-client placeholder icon
        for "no icon assigned" (visually the same question-mark icon
        players see in-game), not a 404. This is treated as NOT resolved
        (see _PLACEHOLDER_SPELL_ICON_SLUGS below) so it's never cached and
        never reported as a found icon - callers (cogs/raid_summary.py's
        _get_spell_icon_url) fall through to Wowhead instead, same as a
        real 404. Before this fix, a placeholder like this looked
        identical to a real resolved icon (any non-None icon_slug was
        trusted), which is why a couple of TRACKED_ABILITY_ICON_SPELL_IDS
        entries (the Judgements) were silently showing Blizzard's generic
        "?" icon instead of falling back to Wowhead's correct one - fixed
        for those specific abilities with a manual TRACKED_ABILITY_ICON_
        EMOJI override (bypasses this method entirely), but this check
        guards every OTHER spell_id lookup this method is ever asked for.

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
            async with aiohttp.ClientSession() as session:
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

    async def diagnose_spell(self, spell_id: int) -> str:
        """
        Step-by-step verbose report for a spell/ability icon lookup -
        mirrors diagnose_item()'s contract, but ALSO dumps the plain spell
        metadata endpoint (/data/wow/spell/{id}), which get_spell_icon()
        itself never calls (that method only hits the media endpoint,
        needing just an icon). That metadata endpoint's own "name" field
        is the fastest way to tell whether a configured spell ID (e.g.
        config.TRACKED_ABILITY_ICON_SPELL_IDS) actually IS the ability
        it's supposed to be under this namespace, or a mismatched/wrong-
        rank ID Blizzard is nonetheless returning A response for - see
        get_spell_icon()'s docstring for the confirmed "inv_misc_
        questionmark" placeholder-icon case this was built to investigate.
        Never raises.
        """
        namespace = _namespace(self.region)
        lines = [f"**Diagnosing spell {spell_id}** (region={self.region}, namespace={namespace})"]

        try:
            token = await self._get_token()
            lines.append(f"✅ OAuth token acquired ({token[:8]}...)")
        except Exception as e:
            lines.append(f"❌ OAuth token request failed: {e!r}")
            return "\n".join(lines)

        headers = {"Authorization": f"Bearer {token}"}
        base = _api_base(self.region)
        params = {"namespace": namespace, "locale": "en_US"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/data/wow/spell/{spell_id}", params=params, headers=headers) as resp:
                    spell_body = await resp.text()
                    lines.append(f"Spell endpoint: HTTP {resp.status}\n```\n{spell_body[:1500]}\n```")

                async with session.get(f"{base}/data/wow/media/spell/{spell_id}", params=params, headers=headers) as resp:
                    media_body = await resp.text()
                    lines.append(f"Spell media endpoint: HTTP {resp.status}\n```\n{media_body[:1500]}\n```")
        except Exception as e:
            lines.append(f"❌ Request failed: {e!r}")
            return "\n".join(lines)

        try:
            result = await self.get_spell_icon(spell_id)
            lines.append(f"Parsed result: `{result}`")
        except Exception as e:
            lines.append(f"❌ Parsing raised (this shouldn't happen - get_spell_icon never raises normally): {e!r}")

        return "\n".join(lines)

    async def get_character_equipment(self, realm_slug: str, character_name: str) -> list:
        """
        Returns a list of equipped items via Blizzard's Character Equipment
        Summary endpoint: GET /profile/wow/character/{realmSlug}/
        {characterName}/equipment, namespace profile-classic-{region} (see
        _profile_namespace) - [{"slot_type", "slot_name", "item_id", "name",
        "quality"}, ...], quality as an int matching get_item()'s
        _QUALITY_TYPE_TO_INT convention. realm_slug needs Blizzard's own
        slug format (lowercase, spaces->hyphens) - callers reuse whatever
        realm slug they already have for WCL (ApplyCog.server_slug), which
        should match for an official Fresh/Anniversary realm (WCL mirrors
        Blizzard's own slug for those), but this is NOT independently
        confirmed live - same caveat as the rest of this file (see the
        module docstring). character_name is lowercased before the request
        per Blizzard's documented convention for profile character
        endpoints specifically (case-sensitive elsewhere).

        Never raises - returns [] on any failure: missing token, a 404
        (character not found under this realm/namespace), or a 403 (some
        profile sub-resources are "Protected" and may need the character
        OWNER's own OAuth consent rather than just app client-credentials,
        or may be gated by an in-game armory-visibility toggle - either
        looks identical to this method, logged but not distinguished
        further). Use diagnose_character() to see exactly which case
        applies for a given character.
        """
        fallback = []
        try:
            token = await self._get_token()
        except Exception:
            log.warning("Couldn't get a Blizzard API token - no equipment data for %s", character_name, exc_info=True)
            return fallback

        headers = {"Authorization": f"Bearer {token}"}
        base = _api_base(self.region)
        namespace = _profile_namespace(self.region)
        params = {"namespace": namespace, "locale": "en_US"}
        slug = character_name.lower()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/profile/wow/character/{realm_slug}/{slug}/equipment",
                    params=params, headers=headers,
                ) as resp:
                    if resp.status in (403, 404):
                        log.warning(
                            "Blizzard API returned %s for %s's equipment (namespace %s) - "
                            "no gear summary available (not found, privacy-protected, or "
                            "wrong realm slug/namespace)",
                            resp.status, character_name, namespace,
                        )
                        return fallback
                    resp.raise_for_status()
                    data = await resp.json()
        except Exception:
            log.warning("Blizzard API equipment lookup failed for %s", character_name, exc_info=True)
            return fallback

        items = []
        for entry in data.get("equipped_items") or []:
            item_ref = entry.get("item") or {}
            slot = entry.get("slot") or {}
            quality_type = ((entry.get("quality") or {}).get("type") or "").upper()
            items.append({
                "slot_type": slot.get("type"),
                "slot_name": slot.get("name"),
                "item_id": item_ref.get("id"),
                "name": entry.get("name"),
                "quality": _QUALITY_TYPE_TO_INT.get(quality_type),
            })
        return items

    async def get_character_specializations(self, realm_slug: str, character_name: str) -> list:
        """
        Best-effort talent build lookup via Blizzard's Character
        Specializations endpoint: GET /profile/wow/character/{realmSlug}/
        {characterName}/specializations, namespace profile-classic-{region}.
        The endpoint name is "specializations", NOT "statistics" -
        .../statistics is a real but unrelated endpoint (raw combat-derived
        counter stats, not talents). Returns one entry per talent group -
        TWO entries for a dual-specced Classic character, one for a
        single-spec character:
            {"is_active": bool, "spec_name": str|None,
             "points_by_tree": [(tree_name, points), ...] | None,
             "total_points": int}

        CONFIRMED live (2026-08, real EU realm/character - see
        _parse_specialization_groups()' docstring for the exact shape
        found): the endpoint exists, returns HTTP 200 under
        profile-classic-{region}, and the real per-talent point field is
        "talent_rank", nested at group["specializations"][*]["talents"] -
        both were originally guessed wrong (as "spent_points" directly
        under group["talents"]) until checked against this real response.

        STILL UNCONFIRMED: no spec-name (e.g. "Fire") or tree-grouping
        field was visible in the response checked, so "spec_name" and the
        "31/10/20" split ("points_by_tree") may come back None/empty even
        though total_points is now correctly computed - but that response
        was also cut off (Discord's/this diagnostic's length limits) before
        showing every talent in the group, so a tree/name field further in
        isn't ruled out either. If Blizzard genuinely never includes tree
        membership per talent, getting the 31/10/20 split would need a
        separate static talent-id -> tree lookup table (not attempted
        here - see _bucket_talent_points' docstring). Use
        diagnose_character() (now dumping a much longer raw body for this
        endpoint specifically) to check a fuller response and adjust
        _parse_specialization_groups/_bucket_talent_points if a real field
        turns up.

        SEPARATELY (not something this method can detect or work around):
        Blizzard's profile API for these transitioned TBC Anniversary
        realms has a live, reported bug where the returned data (gear here
        too, not just talents) can be a stale Classic-Era snapshot rather
        than the character's current state - confirmed 2026-08 against a
        real character whose returned gear was their old Classic Era
        loadout, not their current TBC gear. Nothing server-side or
        client-side here can force a refresh; this is purely a caveat for
        callers to surface to the user (see cogs/apply.py's
        _compute_armory_block, which appends a note about this to the
        rendered block).

        Never raises - returns [] on any failure (token, 403/404 - see
        get_character_equipment()'s docstring for what those can mean).
        """
        fallback = []
        try:
            token = await self._get_token()
        except Exception:
            log.warning("Couldn't get a Blizzard API token - no spec data for %s", character_name, exc_info=True)
            return fallback

        headers = {"Authorization": f"Bearer {token}"}
        base = _api_base(self.region)
        namespace = _profile_namespace(self.region)
        params = {"namespace": namespace, "locale": "en_US"}
        slug = character_name.lower()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/profile/wow/character/{realm_slug}/{slug}/specializations",
                    params=params, headers=headers,
                ) as resp:
                    if resp.status in (403, 404):
                        log.warning(
                            "Blizzard API returned %s for %s's specializations (namespace %s) - "
                            "no talent build data available",
                            resp.status, character_name, namespace,
                        )
                        return fallback
                    resp.raise_for_status()
                    data = await resp.json()
        except Exception:
            log.warning("Blizzard API specializations lookup failed for %s", character_name, exc_info=True)
            return fallback

        return _parse_specialization_groups(data)

    async def diagnose_character(self, realm_slug: str, character_name: str) -> str:
        """
        Step-by-step verbose report for a diagnostic slash command (see
        cogs/apply.py's apply_test_blizzard) - same contract as
        diagnose_item(): dumps raw HTTP status + response bodies for both
        the equipment and specializations endpoints, plus each one's parsed
        result, so a namespace/field-name/realm-slug mismatch is
        immediately visible instead of silently degrading to "no data" the
        way a normal application post would (by design - see
        get_character_equipment()/get_character_specializations()'s
        docstrings). Never raises.
        """
        namespace = _profile_namespace(self.region)
        lines = [
            f"**Diagnosing character {character_name}** (realm_slug={realm_slug}, "
            f"region={self.region}, namespace={namespace})"
        ]

        try:
            token = await self._get_token()
            lines.append(f"✅ OAuth token acquired ({token[:8]}...)")
        except Exception as e:
            lines.append(f"❌ OAuth token request failed: {e!r}")
            return "\n".join(lines)

        headers = {"Authorization": f"Bearer {token}"}
        base = _api_base(self.region)
        params = {"namespace": namespace, "locale": "en_US"}
        slug = character_name.lower()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/profile/wow/character/{realm_slug}/{slug}/equipment",
                    params=params, headers=headers,
                ) as resp:
                    body = await resp.text()
                    lines.append(f"Equipment endpoint: HTTP {resp.status}\n```\n{body[:1500]}\n```")

                async with session.get(
                    f"{base}/profile/wow/character/{realm_slug}/{slug}/specializations",
                    params=params, headers=headers,
                ) as resp:
                    body = await resp.text()
                    # Wider cap than every other diagnostic dump in this file
                    # (deliberately, not an oversight) - a live check
                    # (2026-08, see get_character_specializations()'s
                    # docstring) showed this endpoint's body getting cut off
                    # mid-JSON at 1500 chars, before ever reaching a second
                    # talent group or any field that might identify which of
                    # the 3 trees each talent belongs to (the still-unsolved
                    # part of the "31/10/20" split - see _bucket_talent_
                    # points' docstring). The outer per-message chunking in
                    # cogs/apply.py's apply_test_blizzard already splits
                    # whatever this returns across multiple Discord messages,
                    # so there's no length concern on that end.
                    lines.append(f"Specializations endpoint: HTTP {resp.status}\n```\n{body[:6000]}\n```")
        except Exception as e:
            lines.append(f"❌ Request failed: {e!r}")
            return "\n".join(lines)

        try:
            equipment = await self.get_character_equipment(realm_slug, character_name)
            lines.append(f"Parsed equipment ({len(equipment)} item(s)): `{equipment}`")
        except Exception as e:
            lines.append(f"❌ Equipment parsing raised (this shouldn't happen): {e!r}")

        try:
            specs = await self.get_character_specializations(realm_slug, character_name)
            lines.append(f"Parsed specializations: `{specs}`")
        except Exception as e:
            lines.append(f"❌ Specialization parsing raised (this shouldn't happen): {e!r}")

        return "\n".join(lines)

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

# Icon slugs Blizzard's media endpoint uses as its own generic "no icon
# assigned" placeholder - confirmed live (2026-08) for spells: WoW's real
# in-client question-mark icon, returned with a normal HTTP 200 rather
# than a 404 for at least a couple of TRACKED_ABILITY_ICON_SPELL_IDS
# entries. get_spell_icon() treats any of these as an unresolved lookup
# (not cached, empty icon_url returned) rather than a real match - see
# that method's docstring. Not applied to get_item()'s item-media lookup:
# no evidence item media has this problem (a real TBC item - Dragonspine
# Trophy, id 28830 - was confirmed 2026-08 to resolve its correct, unique
# icon), so this stays spell-specific rather than risking a false
# rejection on a legitimately plain/simple item icon.
_PLACEHOLDER_SPELL_ICON_SLUGS = {"inv_misc_questionmark"}


def _is_resolved(result: dict, item_id: int) -> bool:
    return result.get("icon_slug") is not None and result.get("name") != f"Item #{item_id}"


def _parse_specialization_groups(data: dict) -> list:
    """See get_character_specializations()'s docstring for the honesty
    caveat this whole function operates under. Tries the plausible
    "specialization_groups" key first (a dual-spec-shaped list, each with
    its own is_active flag) - falls back to treating the whole payload as
    one implicit group if that key isn't present, so a single-spec
    response still parses into something instead of an empty list.

    CONFIRMED live (2026-08, real EU character, see get_character_
    specializations()'s docstring): each group's talents are NOT directly
    under group["talents"] as originally guessed - they're one level
    deeper, under group["specializations"][*]["talents"] ("specializations"
    here is an unrelated wrapper list, seemingly schema reuse from retail's
    endpoint where that word means something else; classic responses seen
    so far only ever have exactly one entry in it). Both shapes are
    checked, so this keeps working if a differently-shaped response ever
    turns up. No spec-name field (e.g. "Fire") was visible in the live
    response checked, though it may simply not have been reached before
    that response got cut off at the diagnostic dump's old length limit
    (see diagnose_character()) - group.get("name") is checked too as one
    more plausible spot, cheap to try, unconfirmed either way."""
    groups = data.get("specialization_groups")
    if groups is None:
        groups = [data] if (data.get("talents") or data.get("specializations") or data.get("talent_specialization")) else []

    result = []
    for group in groups:
        spec_info = group.get("talent_specialization") or group.get("specialization") or {}
        spec_name = spec_info.get("name") or group.get("name")

        talents = list(group.get("talents") or [])
        for spec_entry in group.get("specializations") or []:
            talents.extend(spec_entry.get("talents") or [])
            if spec_name is None:
                nested_spec = spec_entry.get("specialization") or spec_entry.get("talent_specialization") or {}
                spec_name = nested_spec.get("name") or spec_entry.get("name")

        # Real per-talent point field is "talent_rank" (confirmed live) -
        # "spent_points" was the original guess, kept as a fallback in case
        # a differently-shaped response ever uses it instead.
        total_points = sum((t.get("talent_rank") or t.get("spent_points") or 0) for t in talents)
        points_by_tree = _bucket_talent_points(talents)

        result.append({
            "is_active": bool(group.get("is_active", len(groups) == 1)),
            "spec_name": spec_name,
            "points_by_tree": points_by_tree,
            "total_points": total_points,
        })
    return result


def _bucket_talent_points(talents: list):
    """Best-effort grouping of a talent-group's per-talent points into
    per-TREE totals (the "31/10/20" split) - returns a list of (tree_name,
    points) tuples in first-seen order, or None if none of the talent
    entries carry a tree-identifying field this recognizes. Checked field
    names, in order: "talent_tree" (dict with a "name") then "tree" (dict
    or plain string) - still guesses, NOT confirmed (unlike the
    talents-location/talent_rank fixes in _parse_specialization_groups
    above): a live response checked 2026-08 didn't show either field on any
    talent, but that response was also cut off before showing every talent
    in the group, so this isn't proven absent either - see
    get_character_specializations()'s docstring and diagnose_character()
    for how to check a fuller response. TBC's 3 talent trees are named
    identically to their matching spec (e.g. the "Arms" tree = the "Arms"
    spec), so a tree's "name" here doubles as the display name with no
    extra mapping needed once/if a real field name is confirmed - see
    _format_talent_split in cogs/apply.py for how the 3 totals get ordered
    to match config.CLASS_SPECS."""
    buckets = {}
    order = []
    found_any_tree_field = False

    for t in talents:
        tree = t.get("talent_tree") or t.get("tree")
        if isinstance(tree, dict):
            tree_name = tree.get("name")
        elif isinstance(tree, str):
            tree_name = tree
        else:
            tree_name = None

        if tree_name is None:
            continue
        found_any_tree_field = True
        if tree_name not in buckets:
            buckets[tree_name] = 0
            order.append(tree_name)
        buckets[tree_name] += t.get("talent_rank") or t.get("spent_points") or 0

    if not found_any_tree_field:
        return None
    return [(name, buckets[name]) for name in order]
