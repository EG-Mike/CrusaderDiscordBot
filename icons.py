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