"""
Rank sync, Discord side: keeps RANK_SYNC_CHANNEL_ID posted with the
members to promote in the game: they have a Discord role (e.g. Fresh) and
claimed a character in the guild whose in-game rank is lower than the
role's rule says (rank_sync.py; rules are set on the dashboard's Rank sync
page). Header: the "Rank sync header" bot message. Every few minutes, and
soon after someone's roles change, it checks whether the list changed and
only then edits the message(s) (channel_posts.py).
"""

import asyncio
import hashlib
import logging
import time
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks

import bot_messages
import channel_posts
import config
import rank_sync
import roster

log = logging.getLogger("wow-apply-bot.rank_sync")

POST_META = "rank_sync_post"
POST_CHECK_MINUTES = 5
POST_LAYOUT = "rank-sync-1"  # part of the post's hash: changing the layout updates old posts
ACCENT = 0xDEAC52


def promote_lines(entries: List[dict], names: Dict[int, str]) -> List[str]:
    """The members to promote, grouped by the rank they should get (highest first)."""
    promote = [e for e in entries if e["status"] == "promote"]
    if not promote:
        return ["-# Nobody to promote right now."]
    lines, current = [], None
    for e in sorted(promote, key=lambda e: (e["target_rank"], roster.display_name(e["character"]))):
        if e["target_rank"] != current:
            current = e["target_rank"]
            count = sum(1 for x in promote if x["target_rank"] == current)
            lines.append(f"### Promote to {rank_sync.rank_label(current, names)} · {count}")
        c = e["character"]
        lines.append(f"**{roster.display_name(c)}** · <@{e['id']}> · now {rank_sync.rank_label(c['guild_rank'], names)}")
    return lines


class RankSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._post_lock = asyncio.Lock()

    async def cog_load(self):
        self.post_loop.start()

    async def cog_unload(self):
        self.post_loop.cancel()

    async def entries(self, guild) -> List[dict]:
        """The server's members with a rule's role or a linked character, grouped (rank_sync.check)."""
        rule_list = await asyncio.to_thread(rank_sync.rules)
        characters = await asyncio.to_thread(roster.characters)
        return rank_sync.check(rank_sync.discord_members(guild), characters, rule_list)

    def channel_problem(self) -> Optional[str]:
        channel_id = int(config.RANK_SYNC_CHANNEL_ID or 0)
        if not channel_id:
            return "No rank sync channel is set (Settings, Rank sync)."
        if self.bot.get_channel(channel_id) is None:
            return "The bot can't see the rank sync channel. Pick another one, or give the bot access."
        if rank_sync.guild_slug() is None:
            return "Set your guild first (Settings, Guild roster)."
        return None

    async def publish(self, *, force: bool = False) -> Optional[str]:
        """Posts or updates the channel when the list changed (or force). Returns what happened, for the dashboard."""
        async with self._post_lock:
            problem = self.channel_problem()
            if problem:
                return problem if force else None
            channel = self.bot.get_channel(int(config.RANK_SYNC_CHANNEL_ID))
            names = await asyncio.to_thread(rank_sync.rank_names)
            lines = promote_lines(await self.entries(channel.guild), names)
            template = bot_messages.current("rank_sync_header")["body"]
            digest = hashlib.sha256("\n".join([POST_LAYOUT, template, *lines]).encode()).hexdigest()
            posted = await asyncio.to_thread(channel_posts.state, POST_META)
            if not force and channel_posts.is_current(posted, channel.id, digest):
                return None
            updated = int(time.time())
            header = bot_messages.text("rank_sync_header", guild=config.GUILD_ROSTER_GUILD or channel.guild.name,
                                       updated=f"<t:{updated}:R>")
            views = []
            for text in channel_posts.chunks(header, lines):
                view = discord.ui.LayoutView(timeout=None)
                view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color(ACCENT)))
                views.append(view)
            problem = await channel_posts.publish(self.bot, POST_META, channel, views, digest, updated, force=force)
            return problem or f"Posted in #{channel.name}."

    def publish_soon(self):
        """After a change on the dashboard or in someone's roles, instead of waiting for the loop."""
        async def run():
            try:
                await self.publish()
            except Exception:
                log.exception("Updating the rank sync channel failed")

        asyncio.ensure_future(run())

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles != after.roles:
            self.publish_soon()

    @tasks.loop(minutes=POST_CHECK_MINUTES)
    async def post_loop(self):
        try:
            await self.publish()
        except Exception:
            log.exception("Updating the rank sync channel failed")

    @post_loop.before_loop
    async def _before_post(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RankSyncCog(bot))
