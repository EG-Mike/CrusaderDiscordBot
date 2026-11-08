"""
Crafting, Discord side: keeps the crafting channel (CRAFTING_CHANNEL_ID)
posted with who in the guild can make what. The rules and data are in
crafting.py, shared with the dashboard's Crafting page, where moderators
pick the featured recipes.

The post has:
  - a header (the "Crafting header" bot message);
  - every profession with its crafters and their skill, highest first
    (CRAFTING_SHOW_PROFESSIONS, at most CRAFTING_CRAFTERS_PER_PROFESSION
    each);
  - each featured recipe, by profession: what it makes (linked like items
    on applications), who knows it (Discord member and character), and the
    materials to send them. Within a profession in a moderator's order, or
    hardest first (crafting.order_key). With CRAFTING_HIDE_UNKNOWN_RECIPES,
    recipes nobody in the guild knows are left out.
Every few minutes it checks whether anything changed and only then edits
the message(s) (channel_posts.py); a long post spans several messages.

Professions and recipes come from character profiles, which
cogs/leveling.py checks. With Leveling off, this cog checks them itself.
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
import crafting
import emoji_library
import gear
import roster

log = logging.getLogger("wow-apply-bot.crafting")

POST_META = "crafting_post"
POST_CHECK_MINUTES = 3
POST_LAYOUT = "crafting-1"   # part of the post's hash: changing the layout updates old posts
PROFILE_CHECK_MINUTES = 10   # only when Leveling (which normally checks profiles) is off
MAX_RECIPE_CRAFTERS = 10
ACCENT = 0xDEAC52


def member_label(guild, c: dict) -> str:
    """'<@member> (Character)' for a linked character, else the character's name."""
    member = guild.get_member(c["discord_id"]) if guild is not None and c.get("discord_id") else None
    name = roster.display_name(c)
    return f"{member.mention} ({name})" if member else name


def heading(icon: Optional[str], text: str) -> str:
    return f"### {icon} {text}" if icon else f"### {text}"


def reagents_text(reagents: List[dict]) -> str:
    return " · ".join(f"{r.get('quantity') or 1}× {r.get('name') or '?'}" for r in reagents)


def profession_lines(professions: List[dict], overview: Dict[int, List[dict]], guild,
                     icons: Dict[int, str]) -> List[str]:
    """Every profession with its crafters, highest skill first; the ones
    nobody has in one line at the end."""
    limit = max(1, int(config.CRAFTING_CRAFTERS_PER_PROFESSION or 10))
    lines, nobody = ["## Professions"], []
    for p in sorted(professions, key=lambda p: p["name"]):
        rows = overview.get(p["id"]) or []
        if not rows:
            nobody.append(p["name"])
            continue
        lines.append(heading(icons.get(p["id"]), f"{p['name']} · {len(rows)} crafter{'s' if len(rows) != 1 else ''}"))
        for row in rows[:limit]:
            lines.append(f"`{row['skill'] if row['skill'] is not None else '?':>3}/{row['max'] or '?'}` "
                         f"{member_label(guild, row['character'])}")
        if len(rows) > limit:
            lines.append(f"-# … and {len(rows) - limit} more on the dashboard's Crafting page")
    if nobody:
        lines.append(f"-# Nobody in the guild has {', '.join(nobody)} yet.")
    return lines


def featured_lines(featured: List[dict], knows: Dict[int, List[dict]], guild, profession_icons: Dict[int, str],
                   recipe_icons: Dict[int, str], *, hide_unknown: bool = False) -> List[str]:
    """Each featured recipe, by profession (in crafting.featured's order):
    what it makes, who knows it and the materials. hide_unknown: leave out
    recipes nobody knows (and a profession left with none)."""
    if hide_unknown:
        featured = [r for r in featured if knows.get(r["recipe_id"])]
    if not featured:
        return []
    lines, current = ["## ⭐ Featured recipes", "-# Send the materials to one of the crafters and ask them here."], None
    for r in featured:
        if r["profession_id"] != current:
            current = r["profession_id"]
            lines.append(heading(profession_icons.get(current), r["profession_name"] or "Other"))
        name = r["item_name"] or r["name"]
        url = gear.item_url(r["item_id"])
        title = f"**[{name}]({url})**" if url else f"**{name}**"
        if r.get("quantity") and r["quantity"] > 1:
            title += f" ×{r['quantity']:g}"
        lines.append(f"{recipe_icons.get(r['recipe_id']) or '▫️'} {title}")
        who = crafting.by_skill(knows.get(r["recipe_id"]) or [], r["profession_id"])
        if who:
            shown = ", ".join(member_label(guild, c) for c in who[:MAX_RECIPE_CRAFTERS])
            more = f" and {len(who) - MAX_RECIPE_CRAFTERS} more" if len(who) > MAX_RECIPE_CRAFTERS else ""
            lines.append(f"-# Crafters: {shown}{more}")
        else:
            lines.append("-# Nobody in the guild knows this recipe yet.")
        if r["reagents"]:
            lines.append(f"-# Materials: {reagents_text(r['reagents'])}")
    return lines


class CraftingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._post_lock = asyncio.Lock()

    async def cog_load(self):
        self.post_loop.start()
        self.profile_loop.start()

    async def cog_unload(self):
        self.post_loop.cancel()
        self.profile_loop.cancel()

    # --- the crafting channel --------------------------------------------------------------------

    def channel_problem(self) -> Optional[str]:
        channel_id = int(config.CRAFTING_CHANNEL_ID or 0)
        if not channel_id:
            return "No crafting channel is set (Settings, Crafting)."
        if self.bot.get_channel(channel_id) is None:
            return "The bot can't see the crafting channel. Pick another one, or give the bot access."
        return None

    async def _icons(self, kind: str, ids) -> Dict[int, str]:
        found = {}
        for game_id in sorted({i for i in ids if i}):
            found[game_id] = await emoji_library.ensure_icon(self.bot, kind, game_id, by="automatic (crafting)")
        return found

    async def publish(self, *, force: bool = False) -> Optional[str]:
        """Posts or updates the crafting channel when its content changed (or
        force). Returns what happened, for the dashboard."""
        async with self._post_lock:
            return await self._publish(force)

    async def _publish(self, force: bool) -> Optional[str]:
        problem = self.channel_problem()
        if problem:
            return problem if force else None
        channel = self.bot.get_channel(int(config.CRAFTING_CHANNEL_ID))
        characters = await asyncio.to_thread(crafting.crafters)
        await crafting.fill_difficulty(self.bot)
        featured = await asyncio.to_thread(crafting.featured)
        try:
            professions = await crafting.professions(self.bot) if config.CRAFTING_SHOW_PROFESSIONS else []
        except crafting.CraftingError as exc:
            log.info("Crafting channel: professions not listed: %s", exc)
            professions = []
        profession_icons = await self._icons("profession", [p["id"] for p in professions]
                                             + [r["profession_id"] for r in featured])
        recipe_icons = await self._icons("recipe", [r["recipe_id"] for r in featured])
        lines = (profession_lines(professions, crafting.profession_overview(characters), channel.guild,
                                  profession_icons) if professions else [])
        lines += featured_lines(featured, crafting.who_knows(characters), channel.guild, profession_icons, recipe_icons,
                                hide_unknown=bool(config.CRAFTING_HIDE_UNKNOWN_RECIPES))
        if not lines:
            lines = ["-# Nothing to show yet: professions appear once members log out in the game, and featured "
                     "recipes are picked on the dashboard (Crafting)."]
        template = bot_messages.current("crafting_header")["body"]
        digest = hashlib.sha256("\n".join([POST_LAYOUT, template, config.GUILD_ROSTER_GUILD or "", *lines])
                                .encode()).hexdigest()
        posted = await asyncio.to_thread(channel_posts.state, POST_META)
        if not force and channel_posts.is_current(posted, channel.id, digest):
            return None
        updated = int(time.time())
        header = bot_messages.text("crafting_header", guild=config.GUILD_ROSTER_GUILD or channel.guild.name,
                                   updated=f"<t:{updated}:R>")
        views = []
        for text in channel_posts.chunks(header, lines):
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color(ACCENT)))
            views.append(view)
        problem = await channel_posts.publish(self.bot, POST_META, channel, views, digest, updated)
        return problem or f"Posted in #{channel.name}."

    def publish_soon(self):
        """Updates the channel after a change on the dashboard instead of waiting for the loop."""
        async def run():
            try:
                await self.publish()
            except Exception:
                log.exception("Updating the crafting channel failed")

        asyncio.ensure_future(run())

    @tasks.loop(minutes=POST_CHECK_MINUTES)
    async def post_loop(self):
        try:
            await self.publish()
        except Exception:
            log.exception("Updating the crafting channel failed")

    @post_loop.before_loop
    async def _before_post(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=PROFILE_CHECK_MINUTES)
    async def profile_loop(self):
        """Professions and recipes come from profile checks, which Leveling
        normally runs; without it, they're checked here."""
        if self.bot.get_cog("LevelingCog") is not None or getattr(self.bot, "blizzard", None) is None:
            return
        try:
            await roster.refresh_profiles(self.bot)
        except Exception:
            log.exception("Checking character profiles for crafting failed")

    @profile_loop.before_loop
    async def _before_profiles(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(CraftingCog(bot))
