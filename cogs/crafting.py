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
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

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
POST_LAYOUT = "crafting-2"   # part of the post's hash: changing the layout updates old posts
PROFILE_CHECK_MINUTES = 10   # only when Leveling (which normally checks profiles) is off
MAX_RECIPE_CRAFTERS = 10
ACCENT = 0xDEAC52


def member_label(guild, c: dict) -> str:
    """'<@member> (Character)' for a linked character, else the character's name."""
    member = guild.get_member(c["discord_id"]) if guild is not None and c.get("discord_id") else None
    name = roster.display_name(c)
    return f"{member.mention} ({name})" if member else name


def reagents_text(reagents: List[dict]) -> str:
    return " · ".join(f"{r.get('quantity') or 1}× {r.get('name') or '?'}" for r in reagents)


def crafter_lines(rows: List[dict], guild) -> List[str]:
    """A profession's crafters with their skill, highest first (at most CRAFTING_CRAFTERS_PER_PROFESSION)."""
    limit = max(1, int(config.CRAFTING_CRAFTERS_PER_PROFESSION or 10))
    lines = [f"`{row['skill'] if row['skill'] is not None else '?':>3}/{row['max'] or '?'}` "
             f"{member_label(guild, row['character'])}" for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"-# … and {len(rows) - limit} more on the dashboard's Crafting page")
    return lines


def recipe_lines(r: dict, knows: Dict[int, List[dict]], guild, recipe_icons: Dict[int, str]) -> List[str]:
    """One featured recipe: what it makes (linked), who knows it and the materials."""
    name = r["item_name"] or r["name"]
    url = gear.item_url(r["item_id"])
    title = f"**[{name}]({url})**" if url else f"**{name}**"
    if r.get("quantity") and r["quantity"] > 1:
        title += f" ×{r['quantity']:g}"
    lines = [f"{recipe_icons.get(r['recipe_id']) or '▫️'} {title}"]
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


def profession_sections(professions: List[dict], overview: Dict[int, List[dict]], featured: List[dict],
                        knows: Dict[int, List[dict]], guild, profession_icons: Dict[int, str],
                        recipe_icons: Dict[int, str], *, show_crafters: bool = True,
                        hide_unknown: bool = False) -> Tuple[List[dict], List[str]]:
    """One section per profession (alphabetically), each its own message in
    the channel: [{"title", "blocks"}], where a block is lines that stay
    together (the crafters, one recipe). A profession is shown when it has
    crafters (with show_crafters) or featured recipes; the professions
    nobody has are returned by name. Featured recipes keep
    crafting.featured's order; hide_unknown leaves out recipes nobody knows."""
    if hide_unknown:
        featured = [r for r in featured if knows.get(r["recipe_id"])]
    names = {p["id"]: p["name"] for p in professions}
    for r in featured:
        names.setdefault(r["profession_id"], r["profession_name"] or "Other")
    sections, nobody = [], []
    for profession_id, name in sorted(names.items(), key=lambda kv: kv[1]):
        crafters = (overview.get(profession_id) or []) if show_crafters else []
        recipes = [r for r in featured if r["profession_id"] == profession_id]
        if not crafters and not recipes:
            if show_crafters and profession_id in {p["id"] for p in professions}:
                nobody.append(name)
            continue
        icon = profession_icons.get(profession_id)
        title = f"## {icon} {name}" if icon else f"## {name}"
        blocks = []
        if crafters:
            blocks.append([f"-# {len(crafters)} crafter{'s' if len(crafters) != 1 else ''} · highest skill first",
                           *crafter_lines(crafters, guild)])
        if recipes:
            blocks.append(["### ⭐ Featured recipes", "-# Send the materials to one of the crafters and ask them."])
            blocks += [recipe_lines(r, knows, guild, recipe_icons) for r in recipes]
        sections.append({"title": title, "blocks": blocks})
    return sections, nobody


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
        sections, nobody = profession_sections(
            professions, crafting.profession_overview(characters), featured, crafting.who_knows(characters),
            channel.guild, profession_icons, recipe_icons, show_crafters=bool(professions),
            hide_unknown=bool(config.CRAFTING_HIDE_UNKNOWN_RECIPES))
        intro = [f"-# Nobody in the guild has {', '.join(nobody)} yet."] if nobody else []
        if not sections:
            intro.append("-# Nothing to show yet: professions appear once members log out in the game, and featured "
                         "recipes are picked on the dashboard (Crafting).")
        template = bot_messages.current("crafting_header")["body"]
        digest = hashlib.sha256(json.dumps([POST_LAYOUT, template, config.GUILD_ROSTER_GUILD or "", intro, sections])
                                .encode()).hexdigest()
        posted = await asyncio.to_thread(channel_posts.state, POST_META)
        if not force and channel_posts.is_current(posted, channel.id, digest):
            return None
        updated = int(time.time())
        header = bot_messages.text("crafting_header", guild=config.GUILD_ROSTER_GUILD or channel.guild.name,
                                   updated=f"<t:{updated}:R>")
        texts = channel_posts.chunks(header, intro)
        for section in sections:  # every profession starts its own message
            texts += channel_posts.split_blocks(section["title"], section["blocks"])
        views = []
        for text in texts:
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color(ACCENT)))
            views.append(view)
        problem = await channel_posts.publish(self.bot, POST_META, channel, views, digest, updated, force=force)
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
