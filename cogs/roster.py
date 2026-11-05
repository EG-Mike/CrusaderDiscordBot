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
"""

import asyncio
import json
import logging
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import roster
import wow_api
from permissions import is_moderator

log = logging.getLogger("wow-apply-bot.roster")

STATUS_META = "roster_last_sync"
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

    async def cog_unload(self):
        self.sync_loop.cancel()

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
