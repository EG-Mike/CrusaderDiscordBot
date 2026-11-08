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
roster posted there: a header (the "Guild roster header" bot message), a
column header row and one row per character (level, class icon, name,
linked member, "alt" for alts, and professions with skill/maximum when
GUILD_ROSTER_POST_PROFESSIONS is on), highest level first. Characters below
GUILD_ROSTER_POST_MIN_LEVEL are left out unless they're someone's main.
Every few minutes it checks whether anything changed and only then edits
the message(s); a long roster spans several messages.

Under the last message are two buttons: "Claim a character" (name and
surname, main or alt; members may claim several characters, only when
GUILD_ROSTER_SELF_SERVICE is on) and "My characters" (make one the main or
an alt, or unlink it). Both answer privately.
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import bot_messages
import channel_posts
import config
import db
import emoji_library
import icons
import roster
import wow_api
from permissions import is_moderator

log = logging.getLogger("wow-apply-bot.roster")

STATUS_META = "roster_last_sync"
POST_META = "roster_post"         # {"channel_id", "message_ids", "hash", "updated"}
POST_CHECK_MINUTES = 2
POST_LAYOUT = "table-3"           # part of the post's hash: changing the layout updates old posts
CLAIM_ID = "roster:claim"
MINE_ID = "roster:mine"
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
        self._post_lock = asyncio.Lock()

    async def cog_load(self):
        persistent = discord.ui.LayoutView(timeout=None)  # answers the buttons on posts from before a restart
        persistent.add_item(ClaimButtons(self, claims=True))
        self.bot.add_view(persistent)
        self.sync_loop.change_interval(minutes=max(15, int(config.GUILD_ROSTER_REFRESH_MINUTES or 60)))
        self.sync_loop.start()
        self.post_loop.start()

    async def cog_unload(self):
        self.sync_loop.cancel()
        self.post_loop.cancel()

    # --- the roster posted in Discord ------------------------------------------------------------

    async def _profession_icons(self, rows: List[dict]) -> Dict[int, str]:
        """{profession id: inline emoji} for the professions on the roster ('' when none can be made)."""
        if not config.GUILD_ROSTER_POST_PROFESSIONS:
            return {}
        ids = {p["id"] for c in rows for p in roster.professions(c, primary_only=True) if p.get("id")}
        found = {}
        for profession_id in sorted(ids):
            found[profession_id] = await emoji_library.ensure_icon(self.bot, "profession", profession_id,
                                                                   by="automatic (guild roster)")
        return found

    @staticmethod
    def _profession_text(c: dict, icons_by_id: Dict[int, str]) -> str:
        """'<icon> 200/225 | <icon> 45/75' (the name when there's no icon)."""
        parts = []
        for p in roster.professions(c, primary_only=True):
            label = icons_by_id.get(p.get("id")) or p.get("name") or "?"
            parts.append(f"{label} {p.get('skill') if p.get('skill') is not None else '?'}/{p.get('max') or '?'}")
        return " | ".join(parts)

    def _post_lines(self, rows: List[dict], ref: dict, guild, profession_icons: Optional[Dict[int, str]] = None
                    ) -> List[str]:
        """A header row and one aligned row per character. Discord has no
        tables, so the level and name columns are padded inline code.
        `profession_icons`: shown when professions are on (see _profession_icons)."""
        if not rows:
            return ["-# Nobody on the roster yet."]
        width = max(len("Character"), *(len(roster.display_name(c)) for c in rows))
        lines = [f"`Lv` ⚔️ `{'Character':<{width}}` **Discord**"]
        for c in rows:
            name = class_name(ref, c["class_id"])
            icon = icons.resolve_class_icon(guild, name) if name else ""
            member = guild.get_member(c["discord_id"]) if guild and c["discord_id"] else None
            who = member.mention if member else "-"
            if member and not c["is_main"]:
                who += " · alt"
            level = c["level"] if c["level"] is not None else "?"
            line = f"`{level:>2}` {icon or '▫️'} `{roster.display_name(c):<{width}}` {who}"
            trades = self._profession_text(c, profession_icons or {}) if config.GUILD_ROSTER_POST_PROFESSIONS else ""
            lines.append(f"{line} · {trades}" if trades else line)
        return lines

    async def publish_roster(self, *, force: bool = False) -> Optional[str]:
        """Posts or updates the roster in the roster channel when its content
        changed (or force). Returns what happened, for the dashboard."""
        async with self._post_lock:
            return await self._publish_roster(force)

    async def _publish_roster(self, force: bool) -> Optional[str]:
        channel_id = int(config.GUILD_ROSTER_CHANNEL_ID or 0)
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return "The bot can't see the roster channel."
        posted = await asyncio.to_thread(channel_posts.state, POST_META)
        rows = await asyncio.to_thread(roster.posted_characters)
        lines = self._post_lines(rows, await reference(self.bot), channel.guild, await self._profession_icons(rows))
        template = bot_messages.current("guild_roster_header")["body"]
        claims = bool(config.GUILD_ROSTER_SELF_SERVICE)
        digest = hashlib.sha256("\n".join([POST_LAYOUT, str(claims), template, config.GUILD_ROSTER_GUILD or "",
                                           *lines]).encode()).hexdigest()
        if not force and channel_posts.is_current(posted, channel_id, digest):
            return None
        updated = int(time.time())
        header = bot_messages.text("guild_roster_header", guild=config.GUILD_ROSTER_GUILD or channel.guild.name,
                                   count=len(rows), updated=f"<t:{updated}:R>")
        views = []
        chunks = channel_posts.chunks(header, lines)
        for i, text in enumerate(chunks):
            view = discord.ui.LayoutView(timeout=None)
            container = discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color(0xDEAC52))
            if i == len(chunks) - 1:
                container.add_item(discord.ui.Separator())
                container.add_item(ClaimButtons(self, claims=claims))
            view.add_item(container)
            views.append(view)

        problem = await channel_posts.publish(self.bot, POST_META, channel, views, digest, updated)
        return problem or f"Posted the roster in #{channel.name}."

    @tasks.loop(minutes=POST_CHECK_MINUTES)
    async def post_loop(self):
        try:
            await self.publish_roster()
        except Exception:
            log.exception("Updating the posted guild roster failed")

    @post_loop.before_loop
    async def _before_post(self):
        await self.bot.wait_until_ready()

    # --- claiming characters from the posted roster ----------------------------------------------

    def _refresh_post_soon(self):
        """Updates the posted roster after a claim instead of waiting for the loop."""
        async def refresh():
            try:
                await self.publish_roster()
            except Exception:
                log.exception("Updating the posted guild roster failed")

        asyncio.ensure_future(refresh())

    async def claim_button(self, interaction: discord.Interaction):
        if not config.GUILD_ROSTER_SELF_SERVICE and not is_moderator(interaction.user):
            return await self._reply(interaction, "Ask a moderator to link your characters.")
        await interaction.response.send_modal(ClaimModal(self))

    async def claim(self, interaction: discord.Interaction, name: str, role: str):
        """The claim modal was submitted: link the character to the member as a main or alt."""
        if not config.GUILD_ROSTER_SELF_SERVICE and not is_moderator(interaction.user):
            return await self._reply(interaction, "Ask a moderator to link your characters.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            found = await roster.find_or_add(self.bot, name)
            # Claiming is for yourself only: someone else's character needs a moderator.
            c = await asyncio.to_thread(roster.link, found["slug"], interaction.user.id, by=str(interaction.user),
                                        as_moderator=False)
            setter = roster.set_main if role == "main" else roster.set_alt
            c = await asyncio.to_thread(setter, c["slug"], discord_id=interaction.user.id, as_moderator=False)
        except (roster.RosterError, wow_api.WowApiError) as exc:
            return await self._reply(interaction, str(exc))
        kind = "your main" if c["is_main"] else "an alt"
        hidden = ""
        if not c["is_main"] and (c["level"] or 0) < int(config.GUILD_ROSTER_POST_MIN_LEVEL or 0):
            hidden = f" Alts show on the roster from level {config.GUILD_ROSTER_POST_MIN_LEVEL}."
        await self._reply(interaction, f"**{roster.display_name(c)}** (level {c['level'] or '?'}) is now linked "
                                       f"to you as {kind}.{hidden}")
        self._refresh_post_soon()

    async def mine_button(self, interaction: discord.Interaction):
        # Acknowledge first: Discord drops an interaction that isn't answered
        # within 3 seconds, and the database read can wait behind other work.
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(roster.characters, discord_id=interaction.user.id)
        view = MyCharactersView(self, interaction.user.id, rows)
        await interaction.followup.send(view.text(), view=view if rows else discord.utils.MISSING,
                                        ephemeral=True, allowed_mentions=NO_PINGS)

    async def change_own(self, interaction: discord.Interaction, character_slug: str, action: str) -> str:
        """Main / alt / unlink from "My characters". Returns the outcome text."""
        functions = {"main": roster.set_main, "alt": roster.set_alt, "unlink": roster.unlink}
        try:
            c = await asyncio.to_thread(functions[action], character_slug, discord_id=interaction.user.id,
                                        as_moderator=False)
        except roster.RosterError as exc:
            return str(exc)
        self._refresh_post_soon()
        done = {"main": "is now your main", "alt": "is now an alt", "unlink": "is no longer linked to you"}[action]
        return f"**{roster.display_name(c)}** {done}."

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


class ClaimButtons(discord.ui.ActionRow):
    """The buttons under the posted roster. Persistent: the custom IDs are
    fixed and the cog registers them at startup."""

    def __init__(self, cog: "RosterCog", *, claims: bool):
        super().__init__()
        self.cog = cog
        if not claims:
            self.remove_item(self.claim)

    @discord.ui.button(label="Claim a character", emoji="🙋", style=discord.ButtonStyle.primary, custom_id=CLAIM_ID)
    async def claim(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.claim_button(interaction)

    @discord.ui.button(label="My characters", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=MINE_ID)
    async def mine(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.mine_button(interaction)


class ClaimModal(discord.ui.Modal, title="Claim a character"):
    def __init__(self, cog: "RosterCog"):
        super().__init__(timeout=600)
        self.cog = cog
        self.character = discord.ui.TextInput(placeholder="e.g. Eggmike Thewhite", min_length=2, max_length=60)
        self.role = discord.ui.Select(options=[
            discord.SelectOption(label="Main", value="main", description="Your main character (one per member)",
                                 emoji="⭐"),
            discord.SelectOption(label="Alt", value="alt", description="One of your other characters", emoji="🔹"),
        ], min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text="Character name and surname", component=self.character))
        self.add_item(discord.ui.Label(text="Main or alt?", component=self.role))

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.claim(interaction, self.character.value, (self.role.values or ["alt"])[0])


class MyCharactersView(discord.ui.View):
    """The member's characters, privately, with main / alt / unlink."""

    def __init__(self, cog: "RosterCog", user_id: int, rows: List[dict]):
        super().__init__(timeout=600)
        self.cog, self.user_id, self.rows = cog, user_id, rows
        self.selected: Optional[str] = rows[0]["slug"] if len(rows) == 1 else None
        self.notice = ""
        self._build()

    def text(self) -> str:
        if not self.rows:
            claim = " Claim one with the button under the roster." if config.GUILD_ROSTER_SELF_SERVICE else ""
            return f"You have no characters linked.{claim}"
        lines = [f"{'⭐' if c['is_main'] else '🔹'} **{roster.display_name(c)}** · level {c['level'] or '?'}"
                 f" · {'main' if c['is_main'] else 'alt'}" for c in self.rows]
        hint = "" if self.selected else "\n-# Pick a character below to change it."
        notice = f"\n\n{self.notice}" if self.notice else ""
        return "**Your characters**\n" + "\n".join(lines) + hint + notice

    def _build(self):
        self.clear_items()
        if not self.rows:
            return
        picker = discord.ui.Select(placeholder="Pick a character", options=[
            discord.SelectOption(label=roster.display_name(c), value=c["slug"], default=c["slug"] == self.selected,
                                 description=f"Level {c['level'] or '?'} · {'main' if c['is_main'] else 'alt'}")
            for c in self.rows[:25]])
        picker.callback = self._pick
        self.add_item(picker)
        current = next((c for c in self.rows if c["slug"] == self.selected), None)
        for action, label, style in (("main", "Make main", discord.ButtonStyle.primary),
                                     ("alt", "Make alt", discord.ButtonStyle.secondary),
                                     ("unlink", "Unlink", discord.ButtonStyle.danger)):
            button = discord.ui.Button(label=label, style=style, disabled=current is None
                                       or (action == "main" and current["is_main"])
                                       or (action == "alt" and not current["is_main"]))
            button.callback = self._make_action(action)
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _pick(self, interaction: discord.Interaction):
        self.selected = interaction.data.get("values", [None])[0]
        self.notice = ""
        self._build()
        await interaction.response.edit_message(content=self.text(), view=self)

    def _make_action(self, action: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()  # within Discord's 3 seconds; the message is edited below
            self.notice = await self.cog.change_own(interaction, self.selected, action)
            self.rows = await asyncio.to_thread(roster.characters, discord_id=self.user_id)
            if self.selected not in {c["slug"] for c in self.rows}:
                self.selected = None
            self._build()
            await interaction.edit_original_response(content=self.text(), view=self, allowed_mentions=NO_PINGS)
        return callback


async def setup(bot: commands.Bot):
    await bot.add_cog(RosterCog(bot))
