"""
Who counts as a moderator in Discord: anyone with the Manage Roles
permission or the moderator role (Settings, General, Moderation). The same
rule the dashboard uses (webapi/auth.py). New cogs use this instead of
their own copy.
"""

import discord

import config


def is_moderator(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_roles:
        return True
    return bool(config.MOD_ROLE_ID) and any(role.id == config.MOD_ROLE_ID for role in member.roles)
