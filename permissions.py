"""
Who counts as a moderator in Discord: anyone with the Manage Roles
permission or the moderator role (Settings, General, Moderation). The same
rule the dashboard uses (webapi/auth.py). Every cog uses these instead of
a copy of its own.
"""

import discord

import config


def is_moderator(member) -> bool:
    """For a member at hand (e.g. interaction.user in a server)."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_roles:
        return True
    return bool(config.MOD_ROLE_ID) and any(role.id == config.MOD_ROLE_ID for role in member.roles)


async def member_is_moderator(guild, user_id: int) -> bool:
    """For a user id: looks the member up (fetching it when it isn't
    cached). False outside a server or when they aren't a member."""
    if guild is None:
        return False
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
    return is_moderator(member)
