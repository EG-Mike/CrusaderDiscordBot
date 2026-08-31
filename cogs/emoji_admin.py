"""
Mod-only tool for growing the bot's library of custom application emoji,
from a pasted Wowhead item/spell link instead of a manual image upload -
the link is only ever used to PARSE OUT the item/spell ID (see
WOWHEAD_LINK_RE), not necessarily as the actual data source.

How it resolves a link (see _resolve_link): for an ITEM link, prefers
self.bot.blizzard (Blizzard's own Game Data API) when configured, falling
back to Wowhead's XML feed otherwise - same prefer-Blizzard pattern
cogs/raid_summary.py's item lookups already use, added 2026-08 after
Wowhead's own fallback-on-failure path was found to silently return a
fake-successful placeholder icon for a failed lookup (see wowhead.py's
module docstring, 2026-08 entry #3) - Blizzard's item data has no such
issue. For a SPELL link, Wowhead's XML feed stays the primary/name
source: Blizzard's classic namespace was confirmed (2026-08, moderator
report + the /raidsummary-test-spell diagnostic) to 404 even for real,
valid TBC spell IDs, so there's no reliable Blizzard name source for
spells the way there is for items - Blizzard's spell-icon lookup is still
tried as a bonus icon-only override on top of Wowhead's name, in case a
particular ability happens to resolve there.

Wowhead's XML endpoint itself: the regular page's og:image meta tag is a
user-submitted SCREENSHOT, not the icon - so this doesn't scrape the page.
Instead it uses Wowhead's long-standing `&xml` data endpoint
(e.g. wowhead.com/item=12345&xml), which many third-party tools have relied
on for years specifically to get an item/spell's icon filename. That
filename then maps onto Wowhead's public icon CDN
(wow.zamimg.com/images/wow/icons/large/<icon>.jpg).

Caveat: the Wowhead half of this wasn't independently verified against a
live request while building it (only confirmed via documented third-party
usage) - if a link fails to resolve, that's the first thing to suspect,
and worth testing with one link before relying on this for a big bulk
batch.
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

    async def _resolve_from_wowhead(self, session: aiohttp.ClientSession, kind: str, item_id: int):
        """Returns (display_name, icon_url) for one item/spell via
        Wowhead's XML feed, or None if it couldn't be resolved. Pure
        Wowhead lookup - see _resolve_link for where Blizzard's Game Data
        API is tried first/alongside this.

        Confirmed live (2026-08): Wowhead's &xml feed 403s for any
        Classic-era access, whether requested via the modern
        www.wowhead.com/<expansion>/ path or the legacy per-expansion
        subdomain (tbc.wowhead.com, classic.wowhead.com, ...) - both
        blocked even with a browser User-Agent. Only the bare, unprefixed
        (retail) domain is reachable, so that's used for every link
        regardless of expansion. Trade-off: an item/spell reworked between
        a Classic-era game and retail can resolve to the wrong icon/name -
        this is exactly why _resolve_link below prefers Blizzard's Classic-
        namespace Game Data API for items instead of trusting this retail
        data whenever Blizzard is configured; spells have no such Blizzard
        alternative (see _resolve_link's docstring) so they're stuck with
        this trade-off for now.
        """
        xml_url = f"https://www.wowhead.com/{kind}={item_id}&xml"
        try:
            async with session.get(xml_url, headers=REQUEST_HEADERS) as resp:
                resp.raise_for_status()
                text = await resp.text()
        except Exception:
            log.exception("Failed to fetch Wowhead data for %s %s", kind, item_id)
            return None

        icon_match = ICON_TAG_RE.search(text)
        if not icon_match:
            return None
        name_match = NAME_TAG_RE.search(text)

        icon_name = icon_match.group(1).strip()
        display_name = name_match.group(1).strip() if name_match else icon_name
        icon_url = f"https://wow.zamimg.com/images/wow/icons/large/{icon_name}.jpg"
        return display_name, icon_url

    async def _resolve_link(self, session: aiohttp.ClientSession, url: str):
        """Returns (display_name, icon_url) for one Wowhead item/spell
        link, or None if it couldn't be resolved. Prefers Blizzard's Game
        Data API (self.bot.blizzard) over Wowhead's XML feed when
        configured, same prefer-Blizzard/fall-back-to-Wowhead pattern
        cogs/raid_summary.py's _get_item_data/_get_spell_icon_url already
        use - but ONLY for items. Blizzard's classic spell endpoint was
        confirmed live (2026-08, via the moderator-only
        /raidsummary-test-spell diagnostic) to 404 even for real, valid
        TBC spell IDs - so unlike items, there's no reliable Blizzard
        source for a spell's NAME (get_spell_icon() deliberately never
        returns one anyway - see its own docstring for why). Spells stay
        on Wowhead's XML fetch as the primary/name source for that reason;
        Blizzard's spell-icon lookup is still tried as a bonus ICON-ONLY
        override on top of Wowhead's name, in case a particular ability
        does resolve there - harmless either way, since a failed Blizzard
        lookup just means Wowhead's own icon is kept, same graceful-
        degradation contract as every other Blizzard-preferring lookup in
        this repo.
        """
        match = WOWHEAD_LINK_RE.search(url)
        if not match:
            return None
        kind, item_id = match.group(1), int(match.group(2))

        if kind == "item" and self.bot.blizzard is not None:
            result = await self.bot.blizzard.get_item(item_id)
            if result.get("icon_slug") is not None:
                return result["name"], result["icon_url"]

        resolved = await self._resolve_from_wowhead(session, kind, item_id)
        if resolved is None:
            return None
        display_name, icon_url = resolved

        if kind == "spell" and self.bot.blizzard is not None:
            blizzard_icon = await self.bot.blizzard.get_spell_icon(item_id)
            if blizzard_icon.get("icon_url"):
                icon_url = blizzard_icon["icon_url"]

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
                resolved = await self._resolve_link(session, url)
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