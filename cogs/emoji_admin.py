"""
Mod-only tool for growing the bot's library of custom application emoji,
sourced from Wowhead item/spell links instead of manual image uploads.

How it resolves a link: Wowhead's regular page's og:image meta tag is a
user-submitted SCREENSHOT, not the icon - so this doesn't scrape the page.
Instead it uses Wowhead's long-standing `&xml` data endpoint
(e.g. wowhead.com/item=12345&xml), which many third-party tools have relied
on for years specifically to get an item/spell's icon filename. That
filename then maps onto Wowhead's public icon CDN
(wow.zamimg.com/images/wow/icons/large/<icon>.jpg).

Caveat: this wasn't independently verified against a live request while
building it (only confirmed via documented third-party usage) - if a link
fails to resolve, that's the first thing to suspect, and worth testing with
one link before relying on this for a big bulk batch.
"""

import os
import re
import logging

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

log = logging.getLogger("wow-apply-bot.emoji_admin")

WOWHEAD_LINK_RE = re.compile(r"wowhead\.com/(?:[a-z0-9-]+/)?(item|spell)=(\d+)", re.IGNORECASE)
ICON_TAG_RE = re.compile(r"<icon[^>]*>([^<]+)</icon>", re.IGNORECASE)
NAME_TAG_RE = re.compile(r"<name[^>]*>([^<]+)</name>", re.IGNORECASE)

# Same fix as wowhead.py: aiohttp's default User-Agent can trigger Wowhead's
# anti-bot challenge page (no <icon>/<name> tags), so use a browser UA.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

MAX_LINKS_PER_CALL = 15  # keep bulk requests away from emoji-creation rate limits


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug[:32] or "icon"


class EmojiAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    async def _resolve_from_wowhead(self, session: aiohttp.ClientSession, url: str):
        """Returns (display_name, icon_url) for one Wowhead item/spell link,
        or None if it couldn't be resolved."""
        match = WOWHEAD_LINK_RE.search(url)
        if not match:
            return None
        kind, item_id = match.group(1), match.group(2)

        # Confirmed live (2026-08): Wowhead's &xml feed 403s for any
        # Classic-era access, whether requested via the modern
        # www.wowhead.com/<expansion>/ path or the legacy per-expansion
        # subdomain (tbc.wowhead.com, classic.wowhead.com, ...) - both
        # blocked even with a browser User-Agent. Only the bare, unprefixed
        # (retail) domain is reachable, so that's used for every link
        # regardless of expansion. Trade-off: an item/spell reworked between
        # a Classic-era game and retail can resolve to the wrong icon/name.
        xml_url = f"https://www.wowhead.com/{kind}={item_id}&xml"
        try:
            async with session.get(xml_url, headers=REQUEST_HEADERS) as resp:
                resp.raise_for_status()
                text = await resp.text()
        except Exception:
            log.exception("Failed to fetch Wowhead data for %s", url)
            return None

        icon_match = ICON_TAG_RE.search(text)
        if not icon_match:
            return None
        name_match = NAME_TAG_RE.search(text)

        icon_name = icon_match.group(1).strip()
        display_name = name_match.group(1).strip() if name_match else icon_name
        icon_url = f"https://wow.zamimg.com/images/wow/icons/large/{icon_name}.jpg"
        return display_name, icon_url

    @app_commands.command(
        name="add-emoji",
        description="Add custom emoji from Wowhead item/spell links (moderator only)",
    )
    @app_commands.describe(
        wowhead_links="One or more Wowhead item/spell links, separated by spaces or newlines"
    )
    async def add_emoji(self, interaction: discord.Interaction, wowhead_links: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only moderators can add emoji.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        urls = [u.strip() for u in wowhead_links.replace(",", " ").split() if u.strip()]
        if not urls:
            await interaction.followup.send("No links found in that message.", ephemeral=True)
            return
        if len(urls) > MAX_LINKS_PER_CALL:
            await interaction.followup.send(
                f"That's {len(urls)} links - please do at most {MAX_LINKS_PER_CALL} at a "
                "time to stay clear of Discord's emoji-creation rate limits.",
                ephemeral=True,
            )
            return

        try:
            existing = await self.bot.fetch_application_emojis()
        except Exception:
            log.exception("Couldn't fetch existing application emojis")
            existing = []
        existing_names = {e.name for e in existing}

        results = []
        async with aiohttp.ClientSession() as session:
            for url in urls:
                resolved = await self._resolve_from_wowhead(session, url)
                if resolved is None:
                    results.append(f"❌ Couldn't resolve: {url}")
                    continue

                display_name, icon_url = resolved
                emoji_name = _slugify(display_name)

                if emoji_name in existing_names:
                    results.append(f"⚠️ Already exists: **{display_name}** (`:{emoji_name}:`)")
                    continue

                try:
                    async with session.get(icon_url) as resp:
                        resp.raise_for_status()
                        image_bytes = await resp.read()
                except Exception:
                    results.append(f"❌ Couldn't download icon for: **{display_name}**")
                    continue

                try:
                    emoji = await self.bot.create_application_emoji(
                        name=emoji_name, image=image_bytes
                    )
                    existing_names.add(emoji_name)
                    results.append(
                        f"✅ {emoji} **{display_name}** - use as `<:{emoji.name}:{emoji.id}>`"
                    )
                except Exception:
                    log.exception("Failed to create application emoji for %s", display_name)
                    results.append(f"❌ Discord rejected the upload for: **{display_name}**")

        await interaction.followup.send("\n".join(results)[:2000], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EmojiAdminCog(bot))