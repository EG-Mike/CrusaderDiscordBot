"""
Guild roster, Discord side: keeps the roster in sync with Blizzard and
lets members (and moderators) link characters with /character. The rules
live in roster.py, shared with the dashboard's Roster page.

  /character link <name> [member]    link a character (to yourself; moderators: to anyone)
  /character unlink <name>           unlink it
  /character main <name>             make it your main
  /character spec <name> <main> [off] set its main spec and off spec
  /character list [member]           a member's characters and levels

The guild roster (Settings: guild name) is read every
GUILD_ROSTER_REFRESH_MINUTES. Character profiles, which carry fresher
levels, are checked by cogs/leveling.py.

With a roster channel set (GUILD_ROSTER_CHANNEL_ID), the bot keeps the
roster posted there: a header (the "Guild roster header" bot message) and
one line per character (level, class icon, name, linked member), highest
level first. Every few minutes it checks whether anything changed and only
then edits the message(s); a long roster spans several messages.
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import bot_messages
import config
import db
import icons
import roster
import wow_api
from permissions import is_moderator

log = logging.getLogger("wow-apply-bot.roster")

STATUS_META = "roster_last_sync"
POST_META = "roster_post"         # {"channel_id", "message_ids", "hash", "updated"}
POST_TEXT_LIMIT = 3800            # per message: Discord allows 4000 characters of text
POST_CHECK_MINUTES = 2
NO_PINGS = discord.AllowedMentions.none()


def last_sync() -> Optional[dict]:
    raw = db.get_meta(STATUS_META) if db.exists() else None
    return json.loads(raw) if raw else None


async def reference(bot) -> dict:
    """Class and spec names (cached by wow_api); empty lists if Blizzard can't be reached."""
    try:
        return await wow_api.reference(bot)
    except wow_api.WowApiError:
        return {"classes": []}


def class_name(ref: dict, class_id) -> str:
    return next((c["name"] for c in ref.get("classes", []) if c["id"] == class_id), "")


def spec_name(ref: dict, spec_id) -> str:
    return next((s["name"] for c in ref.get("classes", []) for s in c["specs"] if s["id"] == spec_id), "")


class RosterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sync_lock = asyncio.Lock()

    async def cog_load(self):
        self.sync_loop.change_interval(minutes=max(15, int(config.GUILD_ROSTER_REFRESH_MINUTES or 60)))
        self.sync_loop.start()
        self.post_loop.start()

    async def cog_unload(self):
        self.sync_loop.cancel()
        self.post_loop.cancel()

    # --- the roster posted in Discord ------------------------------------------------------------

    def _post_lines(self, rows: List[dict], ref: dict, guild) -> List[str]:
        lines = []
        for c in rows:
            name = class_name(ref, c["class_id"])
            icon = icons.resolve_class_icon(guild, name) if name else ""
            member = guild.get_member(c["discord_id"]) if guild and c["discord_id"] else None
            line = f"`{c['level'] if c['level'] is not None else '?':>2}` {icon} **{roster.display_name(c)}**"
            lines.append(line.replace("  ", " ") + (f" · {member.mention}" if member else ""))
        return lines or ["-# Nobody on the roster yet."]

    @staticmethod
    def _chunks(header: str, lines: List[str]) -> List[str]:
        """The posted text split over messages, each within Discord's limit."""
        chunks, current = [], header
        for line in lines:
            if len(current) + len(line) + 1 > POST_TEXT_LIMIT:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        chunks.append(current)
        return chunks

    async def publish_roster(self, *, force: bool = False) -> Optional[str]:
        """Posts or updates the roster in the roster channel when its content
        changed (or force). Returns what happened, for the dashboard."""
        channel_id = int(config.GUILD_ROSTER_CHANNEL_ID or 0)
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return "The bot can't see the roster channel."
        state = json.loads(await asyncio.to_thread(db.get_meta, POST_META) or "{}")
        rows = await asyncio.to_thread(roster.posted_characters)
        lines = self._post_lines(rows, await reference(self.bot), channel.guild)
        template = bot_messages.current("guild_roster_header")["body"]
        digest = hashlib.sha256("\n".join([template, config.GUILD_ROSTER_GUILD or "", *lines]).encode()).hexdigest()
        if not force and state.get("hash") == digest and state.get("channel_id") == channel_id:
            return None
        updated = int(time.time())
        header = bot_messages.text("guild_roster_header", guild=config.GUILD_ROSTER_GUILD or channel.guild.name,
                                   count=len(rows), updated=f"<t:{updated}:R>")
        views = []
        for text in self._chunks(header, lines):
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color(0xDEAC52)))
            views.append(view)

        old_ids = state.get("message_ids", [])
        if state.get("channel_id") != channel_id:
            await self._delete_posted(state.get("channel_id"), old_ids)
            old_ids = []
        try:
            posted = await self._edit_or_send(channel, views, old_ids)
        except discord.NotFound:  # someone deleted one of them: post the roster again, in order
            await self._delete_posted(channel_id, old_ids)
            posted = await self._edit_or_send(channel, views, [])
        except discord.HTTPException as exc:
            log.warning("Couldn't post the guild roster in #%s: %s", channel.name, exc.text or exc.status)
            return "Discord refused the roster message; check the bot's permissions in the roster channel."
        await asyncio.to_thread(db.set_meta, POST_META, json.dumps(
            {"channel_id": channel_id, "message_ids": posted, "hash": digest, "updated": updated}))
        return f"Posted the roster in #{channel.name}."

    async def _edit_or_send(self, channel, views, old_ids) -> List[int]:
        posted = []
        for i, view in enumerate(views):
            if i < len(old_ids):
                await channel.get_partial_message(old_ids[i]).edit(view=view, allowed_mentions=NO_PINGS)
                posted.append(old_ids[i])
            else:
                posted.append((await channel.send(view=view, allowed_mentions=NO_PINGS)).id)
        await self._delete_posted(channel.id, old_ids[len(views):])
        return posted

    async def _delete_posted(self, channel_id, message_ids):
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        for message_id in message_ids if channel else []:
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass

    @tasks.loop(minutes=POST_CHECK_MINUTES)
    async def post_loop(self):
        try:
            await self.publish_roster()
        except Exception:
            log.exception("Updating the posted guild roster failed")

    @post_loop.before_loop
    async def _before_post(self):
        await self.bot.wait_until_ready()

    # --- syncing ---------------------------------------------------------------------------------

    async def sync(self) -> Optional[dict]:
        """Reads the guild roster now (also used by the dashboard). Records
        how it went for the dashboard; returns roster.sync_guild's result."""
        if getattr(self.bot, "blizzard", None) is None:
            return None
        async with self._sync_lock:
            try:
                result = await roster.sync_guild(self.bot)
                status = {"at": roster.timestamp(), "ok": True, "members": result["members"] if result else None,
                          "message": None if result else "No guild name set (Settings, Guild roster)."}
            except (roster.RosterError, wow_api.WowApiError) as exc:
                result, status = None, {"at": roster.timestamp(), "ok": False, "message": str(exc)}
                log.warning("Guild roster sync: %s", exc)
            except Exception:
                log.exception("Guild roster sync failed")
                result, status = None, {"at": roster.timestamp(), "ok": False,
                                        "message": "Something went wrong; see the System log."}
            await asyncio.to_thread(db.set_meta, STATUS_META, json.dumps(status))
            return result

    @tasks.loop(minutes=60)
    async def sync_loop(self):
        await self.sync()

    @sync_loop.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()

    # --- helpers ---------------------------------------------------------------------------------

    def _target(self, interaction: discord.Interaction, member: Optional[discord.Member]):
        """Whose characters this is about, or an error text."""
        mod = is_moderator(interaction.user)
        if member is not None and member.id != interaction.user.id and not mod:
            return None, mod, "Only moderators can manage someone else's characters."
        if (member is None or member.id == interaction.user.id) and not mod and not config.GUILD_ROSTER_SELF_SERVICE:
            return None, mod, "Ask a moderator to link your characters."
        return (member or interaction.user), mod, None

    async def _autocomplete(self, interaction: discord.Interaction, current: str, *, own_only: bool):
        mod = is_moderator(interaction.user)
        rows = await asyncio.to_thread(roster.characters)
        needle = current.lower().strip()
        choices = []
        for c in rows:
            if own_only and not mod and c["discord_id"] != interaction.user.id:
                continue
            if not own_only and c["discord_id"] and c["discord_id"] != interaction.user.id and not mod:
                continue
            label = roster.display_name(c)
            if needle and needle not in label.lower():
                continue
            choices.append(app_commands.Choice(name=f"{label} ({c['level'] or '?'})", value=c["slug"]))
            if len(choices) == 25:
                break
        return choices

    async def link_autocomplete(self, interaction, current: str) -> List[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current, own_only=False)

    async def own_autocomplete(self, interaction, current: str) -> List[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current, own_only=True)

    async def spec_autocomplete(self, interaction, current: str) -> List[app_commands.Choice[str]]:
        character = await asyncio.to_thread(roster.get, getattr(interaction.namespace, "name", "") or "")
        ref = await reference(self.bot)
        specs = next((c["specs"] for c in ref.get("classes", []) if character and c["id"] == character["class_id"]), [])
        return [app_commands.Choice(name=s["name"], value=str(s["id"])) for s in specs
                if current.lower() in s["name"].lower()][:25]

    async def _reply(self, interaction: discord.Interaction, text: str):
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # --- commands --------------------------------------------------------------------------------

    character = app_commands.Group(name="character", description="Your WoW characters on the guild roster")

    @character.command(name="link", description="Link a WoW character to you (moderators: to anyone)")
    @app_commands.describe(name="The character's name and surname, e.g. Eggmike Thewhite",
                           member="Moderators: whose character it is")
    @app_commands.autocomplete(name=link_autocomplete)
    async def link(self, interaction: discord.Interaction, name: str, member: Optional[discord.Member] = None):
        target, mod, error = self._target(interaction, member)
        if error:
            return await self._reply(interaction, error)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            found = await roster.find_or_add(self.bot, name)
            c = await asyncio.to_thread(roster.link, found["slug"], target.id, by=str(interaction.user),
                                        as_moderator=mod)
        except (roster.RosterError, wow_api.WowApiError) as exc:
            return await self._reply(interaction, str(exc))
        whose = "your" if target.id == interaction.user.id else f"{target.mention}'s"
        main = " It's the main." if c["is_main"] else ""
        await self._reply(interaction, f"Linked **{roster.display_name(c)}** (level {c['level'] or '?'}) as "
                                       f"{whose} character.{main}")

    @character.command(name="unlink", description="Unlink one of your characters")
    @app_commands.describe(name="The character")
    @app_commands.autocomplete(name=own_autocomplete)
    async def unlink(self, interaction: discord.Interaction, name: str):
        try:
            c = await asyncio.to_thread(roster.unlink, wow_api.slug(name), discord_id=interaction.user.id,
                                        as_moderator=is_moderator(interaction.user))
        except roster.RosterError as exc:
            return await self._reply(interaction, str(exc))
        await self._reply(interaction, f"Unlinked **{roster.display_name(c)}**.")

    @character.command(name="main", description="Make one of your characters your main")
    @app_commands.describe(name="The character")
    @app_commands.autocomplete(name=own_autocomplete)
    async def main(self, interaction: discord.Interaction, name: str):
        try:
            c = await asyncio.to_thread(roster.set_main, wow_api.slug(name), discord_id=interaction.user.id,
                                        as_moderator=is_moderator(interaction.user))
        except roster.RosterError as exc:
            return await self._reply(interaction, str(exc))
        await self._reply(interaction, f"**{roster.display_name(c)}** is now the main.")

    @character.command(name="spec", description="Set a character's main spec and off spec")
    @app_commands.describe(name="The character", main_spec="Main spec", off_spec="Off spec (optional)")
    @app_commands.autocomplete(name=own_autocomplete, main_spec=spec_autocomplete, off_spec=spec_autocomplete)
    async def spec(self, interaction: discord.Interaction, name: str, main_spec: str, off_spec: Optional[str] = None):
        ref = await reference(self.bot)
        class_specs = {c["id"]: [s["id"] for s in c["specs"]] for c in ref.get("classes", [])}
        try:
            main_id = int(main_spec)
            off_id = int(off_spec) if off_spec else None
        except ValueError:
            return await self._reply(interaction, "Pick the specs from the list.")
        try:
            c = await asyncio.to_thread(roster.set_specs, wow_api.slug(name), main_id, off_id,
                                        discord_id=interaction.user.id, as_moderator=is_moderator(interaction.user),
                                        class_specs=class_specs)
        except roster.RosterError as exc:
            return await self._reply(interaction, str(exc))
        off = f", off spec {spec_name(ref, off_id)}" if off_id else ""
        await self._reply(interaction, f"**{roster.display_name(c)}**: main spec {spec_name(ref, main_id)}{off}.")

    @character.command(name="list", description="Show a member's characters and levels")
    @app_commands.describe(member="Whose characters (default: yours)")
    async def list_characters(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        rows = await asyncio.to_thread(roster.characters, discord_id=target.id)
        whose = "You have" if target.id == interaction.user.id else f"{target.display_name} has"
        if not rows:
            hint = " Link one with /character link." if target.id == interaction.user.id else ""
            return await self._reply(interaction, f"{whose} no characters linked.{hint}")
        ref = await reference(self.bot)
        lines = []
        for c in rows:
            specs = " / ".join(filter(None, [spec_name(ref, c["main_spec_id"]), spec_name(ref, c["off_spec_id"])]))
            details = " · ".join(filter(None, [f"level {c['level'] or '?'}", class_name(ref, c["class_id"]), specs]))
            lines.append(f"{'⭐ ' if c['is_main'] else '• '}**{roster.display_name(c)}**: {details}")
        plural = "s" if len(rows) != 1 else ""
        await self._reply(interaction, f"{whose} {len(rows)} character{plural}:\n" + "\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(RosterCog(bot))
