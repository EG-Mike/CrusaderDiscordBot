"""
Leveling: notices level-ups and congratulates members on milestones, and
shows a leaderboard. Builds on the guild roster (roster.py; cogs/roster.py
reads the guild roster itself).

Every LEVELING_REFRESH_MINUTES the characters' profiles are checked on
Blizzard (from level 10; updated when a player logs out), and every
milestone reached since (LEVELING_MILESTONES) is posted in
LEVELING_CHANNEL_ID with the "Level milestone" text (bot_messages.py,
editable on the dashboard). Milestones found in the same check are grouped:
one line each, combined into as few messages as fit (after a restart that
can be many at once). Alts are skipped unless LEVELING_ANNOUNCE_ALTS;
level-ups older than a day (e.g. the bot was down) aren't announced late.

  /leaderboard [scope] [size]    the highest levels in the guild
"""

import asyncio
import logging
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

import bot_messages
import config
import roster
from cogs.roster import class_name, reference

log = logging.getLogger("wow-apply-bot.leveling")

MENTION_MEMBERS_ONLY = discord.AllowedMentions(everyone=False, roles=False, users=True)
MESSAGE_LIMIT = 2000  # Discord's limit for a message's text


def group_lines(lines: List[Tuple[int, str]], limit: int = MESSAGE_LIMIT) -> List[Tuple[List[int], str]]:
    """(event id, text) pairs combined into messages of at most limit
    characters: [(event ids, message text)]. A text longer than the limit
    on its own is cut short."""
    messages: List[Tuple[List[int], str]] = []
    for event_id, text in lines:
        text = text[:limit]
        if messages and len(messages[-1][1]) + 1 + len(text) <= limit:
            ids, current = messages[-1]
            messages[-1] = (ids + [event_id], f"{current}\n{text}")
        else:
            messages.append(([event_id], text))
    return messages


class LevelingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()

    async def cog_load(self):
        self.check_loop.change_interval(minutes=max(2, int(config.LEVELING_REFRESH_MINUTES or 5)))
        self.check_loop.start()

    async def cog_unload(self):
        self.check_loop.cancel()

    # --- checking and announcing ------------------------------------------------------------------

    async def check_now(self) -> dict:
        """Checks profiles, then posts any milestones (also used by the dashboard)."""
        if getattr(self.bot, "blizzard", None) is None:
            return {"checked": 0, "failed": 0, "events": [], "announced": 0}
        async with self._lock:
            result = await roster.refresh_profiles(self.bot)
            result["announced"] = await self.announce_pending()
            return result

    async def announce_pending(self) -> int:
        events = await asyncio.to_thread(roster.pending_announcements)
        if not events:
            return 0
        channel = self.bot.get_channel(int(config.LEVELING_CHANNEL_ID or 0))
        ref = await reference(self.bot)
        lines = []
        for event in events:
            reason = roster.should_announce(event)
            if reason is None and channel is None:
                reason = "no channel"
            if reason:
                await asyncio.to_thread(roster.mark_announced, event["id"], reason)
                continue
            member = channel.guild.get_member(event["discord_id"]) if event["discord_id"] else None
            lines.append((event["id"], bot_messages.text(
                "level_milestone", member=member.mention if member else event["name"],
                character=roster.display_name(event), level=event["new_value"], milestone=event["milestone"],
                **{"class": class_name(ref, event["class_id"])})))
        posted = 0
        for event_ids, text in group_lines(lines):
            try:
                await channel.send(text, allowed_mentions=MENTION_MEMBERS_ONLY)
            except discord.HTTPException as exc:
                log.warning("Couldn't post level milestones in #%s: %s", getattr(channel, "name", "?"), exc)
                break  # try again next time (until they're a day old)
            for event_id in event_ids:
                await asyncio.to_thread(roster.mark_announced, event_id, "announced")
            posted += len(event_ids)
        return posted

    @tasks.loop(minutes=5)
    async def check_loop(self):
        try:
            await self.check_now()
        except Exception:
            log.exception("Checking for level-ups failed")

    @check_loop.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)  # let the roster cog read the guild first

    # --- leaderboard -----------------------------------------------------------------------------

    @app_commands.command(name="leaderboard", description="The highest-level characters in the guild")
    @app_commands.describe(scope="Mains only, or every character", size="How many to show (default 10)")
    @app_commands.choices(scope=[app_commands.Choice(name="Mains (and characters nobody linked yet)", value="mains"),
                                 app_commands.Choice(name="Every character", value="all")])
    async def leaderboard(self, interaction: discord.Interaction, scope: Optional[str] = "mains",
                          size: app_commands.Range[int, 3, 25] = 10):
        rows = await asyncio.to_thread(roster.leaderboard, size, mains_only=scope != "all")
        if not rows:
            await interaction.response.send_message("Nobody is on the roster yet.", ephemeral=True)
            return
        ref = await reference(self.bot)
        lines = []
        for place, c in enumerate(rows, start=1):
            member = interaction.guild.get_member(c["discord_id"]) if interaction.guild and c["discord_id"] else None
            who = f" ({member.mention})" if member else ""
            details = f"{c['level']} {class_name(ref, c['class_id'])}".strip()
            lines.append(f"`{place:>2}.` **{roster.display_name(c)}** · {details}{who}")
        embed = discord.Embed(title="Leveling leaderboard", description="\n".join(lines), color=0xDEAC52)
        embed.set_footer(text="Levels update when a player logs out.")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))
