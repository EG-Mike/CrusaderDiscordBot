"""
Icon resolution, shared by any feature that needs to show a class/role/spec
icon inline in Discord text.

All three icon types (class, role, spec) use the same mechanism: bot-owned
"application emoji". Instead of manually uploading icons to your server, you
put a stable image URL per icon in config.py (CLASS_ICON_URLS,
ROLE_ICON_URLS, SPEC_ICON_URLS), and on startup the bot downloads each one
once and uploads it as an emoji owned by the bot itself - visible in every
server the bot is in, no manual server upload needed.

Guild emoji (CLASS_EMOJI_NAMES / ROLE_EMOJI_NAMES in config.py) are kept as
a fallback for anyone who already manually uploaded class/role icons before
this existed, or who just prefers managing them that way - if a URL isn't
configured for a class/role, the bot looks for a same-named guild emoji
before giving up and falling back to plain text. Nothing here ever raises;
a missing/failed icon just means plain text for that one item.
"""

import logging
import discord
import aiohttp

import config

log = logging.getLogger("wow-apply-bot.icons")

# Cache of namespaced key -> discord.Emoji (application-owned), e.g.
# "class:Mage", "role:Tank", "spec:Mage:Arcane".
_app_emoji_cache: dict[str, discord.Emoji] = {}


def resolve_guild_emoji(guild: discord.Guild, emoji_name: str | None) -> str:
    """Looks up a custom guild emoji by name. Returns '' if not found/None -
    never raises, so callers can always safely prefix text with this."""
    if not emoji_name:
        return ""
    emoji = discord.utils.get(guild.emojis, name=emoji_name)
    return str(emoji) if emoji else ""


def _resolve_app_emoji(key: str) -> str:
    emoji = _app_emoji_cache.get(key)
    return str(emoji) if emoji else ""


def resolve_class_icon(guild: discord.Guild, class_name: str) -> str:
    icon = _resolve_app_emoji(f"class:{class_name}")
    if icon:
        return icon
    return resolve_guild_emoji(guild, config.CLASS_EMOJI_NAMES.get(class_name))


def resolve_role_icon(guild: discord.Guild, role_name: str) -> str:
    icon = _resolve_app_emoji(f"role:{role_name}")
    if icon:
        return icon
    return resolve_guild_emoji(guild, config.ROLE_EMOJI_NAMES.get(role_name))


def resolve_spec_icon(class_name: str, spec_name: str) -> str:
    return _resolve_app_emoji(f"spec:{class_name}:{spec_name}")


async def ensure_item_emoji(client: discord.Client, existing_by_name: dict, item_id: int, icon_url: str) -> str:
    """
    Lazily provisions (or reuses) a bot-owned application emoji for a WoW
    item's icon, keyed by item ID - used by cogs/raid_summary.py to prefix
    loot lines with a real icon instead of a generic colored square. Same
    underlying mechanism as /add-emoji (cogs/emoji_admin.py) and
    provision_app_emojis() below, just provisioned on demand per item
    instead of all at once at startup (the class/role/spec set is small and
    fixed; the set of items that drop is not).

    `existing_by_name` should be fetched ONCE per command run (via
    `await client.fetch_application_emojis()`) and passed in for every item
    looked up in that run - avoids one full application-emoji-list fetch per
    item. This function keeps it updated as it creates new emoji, so a
    second lookup in the same run (e.g. the same trinket won by two people)
    never re-creates one already made earlier in that same run.

    Returns the emoji as an inline-usable string (e.g. "<:item_30023:123...>"),
    or '' if it couldn't be created - never raises, a missing icon just
    means that loot line has no emoji prefix.
    """
    key = f"item:{item_id}"
    cached = _app_emoji_cache.get(key)
    if cached:
        return str(cached)

    emoji_name = _emoji_safe_name(key)
    if emoji_name in existing_by_name:
        _app_emoji_cache[key] = existing_by_name[emoji_name]
        return str(existing_by_name[emoji_name])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(icon_url) as resp:
                resp.raise_for_status()
                image_bytes = await resp.read()
        emoji = await client.create_application_emoji(name=emoji_name, image=image_bytes)
    except Exception:
        log.warning("Couldn't provision item emoji for item %s", item_id, exc_info=True)
        return ""

    _app_emoji_cache[key] = emoji
    existing_by_name[emoji_name] = emoji
    return str(emoji)


async def provision_app_emojis(client: discord.Client):
    """
    Call once from on_ready. For every configured icon URL across
    config.CLASS_ICON_URLS, config.ROLE_ICON_URLS, and config.SPEC_ICON_URLS,
    ensures a matching bot-owned application emoji exists, downloading +
    uploading it if needed. Safe to call every start - already-provisioned
    emoji are reused rather than re-uploaded.
    """
    try:
        existing = await client.fetch_application_emojis()
    except Exception:
        log.exception(
            "Couldn't fetch application emojis - class/role/spec icons will fall "
            "back to plain text (or guild emoji, for class/role). This needs "
            "discord.py 2.5+; check your installed version."
        )
        existing = []

    existing_by_name = {e.name: e for e in existing}

    sources = {
        "class": config.CLASS_ICON_URLS,
        "role": config.ROLE_ICON_URLS,
        "spec": config.SPEC_ICON_URLS,
    }

    async with aiohttp.ClientSession() as session:
        for prefix, url_map in sources.items():
            for name, url in url_map.items():
                key = f"{prefix}:{name}"
                emoji_name = _emoji_safe_name(key)

                if emoji_name in existing_by_name:
                    _app_emoji_cache[key] = existing_by_name[emoji_name]
                    continue

                try:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        image_bytes = await resp.read()
                except Exception:
                    log.warning("Couldn't download icon for %s from %s", key, url)
                    continue

                try:
                    emoji = await client.create_application_emoji(name=emoji_name, image=image_bytes)
                    _app_emoji_cache[key] = emoji
                    log.info("Provisioned application emoji for %s", key)
                except Exception:
                    log.exception(
                        "Couldn't create application emoji for %s - falling back "
                        "to plain text (or guild emoji, for class/role).", key
                    )


def _emoji_safe_name(key: str) -> str:
    """Discord emoji names: alphanumeric + underscore only. Matches the
    original naming scheme (e.g. "spec:Mage:Arcane" -> "spec_mage_arcane")
    so any spec icons already provisioned under the old scheme are reused
    rather than re-uploaded as orphaned duplicates."""
    return "".join(c if c.isalnum() else "_" for c in key).lower()