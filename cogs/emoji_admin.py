"""
/add-emoji: add WoW item/spell icons to the bot's emoji, from pasted Wowhead
links (or "item:123" / "spell:456"). The links only supply the item/spell
ID; names and icons come from emoji_library.py (Blizzard's API first), which
keeps one emoji per icon image and reuses it for every item/spell sharing
that icon. The dashboard's Emoji page does the same with name search.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import emoji_library

log = logging.getLogger("wow-apply-bot.emoji_admin")

MAX_LINKS_PER_CALL = 15  # keep bulk requests away from emoji-creation rate limits

STATUS_LINES = {
    "created": "✅ {emoji} **{name}**: new emoji `:{emoji_name}:`",
    "shared": "♻️ {emoji} **{name}**: same icon as {others}, reusing `:{emoji_name}:`",
    "exists": "☑️ {emoji} **{name}**: already has `:{emoji_name}:`",
}


def describe(result: dict) -> str:
    emoji, item = result["emoji"], result["item"]
    others = [a["name"] for a in emoji["aliases"] if (a["kind"], a["game_id"]) != (item["kind"], item["game_id"])]
    name = item.get("name") or next((a["name"] for a in emoji["aliases"]
                                     if (a["kind"], a["game_id"]) == (item["kind"], item["game_id"])), "")
    return STATUS_LINES[result["status"]].format(
        emoji=emoji["text"], name=name or f"{item['kind']} {item['game_id']}", emoji_name=emoji["name"],
        others=", ".join(others[:3]) or "another item",
    )


class EmojiAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        if guild is None:
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return False
        if member.guild_permissions.manage_roles:
            return True
        return bool(config.MOD_ROLE_ID) and any(role.id == config.MOD_ROLE_ID for role in member.roles)

    @app_commands.command(name="add-emoji", description="Add item/spell icons as bot emoji from Wowhead links (moderator only)")
    @app_commands.describe(wowhead_links="One or more Wowhead item/spell links (or item:123 / spell:456), separated by spaces")
    async def add_emoji(self, interaction: discord.Interaction, wowhead_links: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can add emoji.", ephemeral=True)
            return
        refs = emoji_library.parse_references(wowhead_links)
        if not refs:
            await interaction.response.send_message(
                "No item or spell links found. Paste Wowhead links like wowhead.com/spell=2061, or item:19019. "
                "To search by name, use the dashboard's Emoji page.", ephemeral=True)
            return
        if len(refs) > MAX_LINKS_PER_CALL:
            await interaction.response.send_message(
                f"That's {len(refs)} links - please do at most {MAX_LINKS_PER_CALL} at a time.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        by = f"{getattr(interaction.user, 'display_name', None) or interaction.user.name} ({interaction.user.id})"
        lines = []
        for kind, game_id in refs:
            try:
                lines.append(describe(await emoji_library.add(self.bot, kind, game_id, by)))
            except emoji_library.EmojiError as exc:
                lines.append(f"❌ {kind} {game_id}: {exc}")
        await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EmojiAdminCog(bot))
