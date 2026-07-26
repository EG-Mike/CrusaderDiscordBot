"""
Attendance tracking for Regular-role eligibility.

Design, per discussion:
  - Rather than trying to auto-detect which WCL report is "the" main raid
    each week (unreliable - can't distinguish it from alt runs/other raid
    nights by zone or day alone, and upload discipline can't be guaranteed
    since raiders other than mods upload too), moderators explicitly curate
    a list of tagged main-raid logs. Only the most recent ATTENDANCE_WINDOW
    entries in that list count toward eligibility - no need for "skipped
    week" or "moved to a different day" logic, since the list itself is
    exactly what mods say it is.
  - A member's "character name" for roster-matching is their current server
    nickname by default - the approval flow (cogs/apply.py) already sets
    this to their main character's name on approval. /checkattendance
    setmain overrides this per-member for the rare case where it's wrong.
    Alts are linked explicitly via /checkattendance link.
  - Every mod action (add/remove log, exclude/include, etc.) is available
    both as a slash command AND a button on the relevant pinned message -
    the buttons call the exact same underlying methods as the commands, so
    there's one source of truth for each action, not two.
  - This is a self-contained feature file, same as apply.py/announcements.py -
    add a new cog for the next feature rather than extending this one.
"""

import os
import re
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import icons

log = logging.getLogger("wow-apply-bot.attendance")

# Sentinel keys in the shared bot.store (same generic JSON store used
# elsewhere - these aren't real Discord message/application records, just
# reusing the same schema-free set()/get()/update() mechanism).
LOG_LIST_KEY = "attendance_log_list"
ALT_LINKS_KEY = "attendance_alt_links"
MAIN_OVERRIDES_KEY = "attendance_main_overrides"
EXCLUDED_KEY = "attendance_excluded"
BASELINE_KEY = "attendance_baseline_overrides"
OVERVIEW_MESSAGE_KEY = "attendance_overview_message"
ROSTER_MESSAGE_KEY = "attendance_roster_message"
EXCLUDED_MESSAGE_KEY = "attendance_excluded_message"
EXPLAINER_MESSAGE_KEY = "attendance_explainer_message"

LOG_LIST_MARKER = "attendance-log-list"
ROSTER_MARKER = "attendance-roster"
EXCLUDED_MARKER = "attendance-excluded"
EXPLAINER_MARKER = "attendance-explainer"

REPORT_LINK_RE = re.compile(r"(?:reports/|^)([A-Za-z0-9]{8,20})(?:[/#].*)?$")

REFRESH_COOLDOWN_SECONDS = 300  # 5 minutes - button only, /checkattendance run is never limited


def _extract_report_code(link: str) -> str:
    """Accepts a bare report code or a full WCL report URL."""
    link = link.strip().rstrip("/")
    match = REPORT_LINK_RE.search(link)
    return match.group(1) if match else link


# --- Modals ---------------------------------------------------------------

class AddLogModal(discord.ui.Modal, title="Add Main Raid Log"):
    date = discord.ui.TextInput(label="Date (e.g. 2026-07-29)", required=True, max_length=32)
    tier = discord.ui.TextInput(label="Tier/zone (e.g. SSC/TK)", required=True, max_length=64)
    link = discord.ui.TextInput(label="WCL report link or code", required=True, max_length=200)

    def __init__(self, cog: "AttendanceCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmation = await self.cog._do_addlog(
            interaction.guild, str(self.date), str(self.tier), str(self.link)
        )
        await interaction.followup.send(confirmation, ephemeral=True)


class RemoveLogModal(discord.ui.Modal, title="Remove Main Raid Log"):
    log_id = discord.ui.TextInput(label="Log #ID to remove", required=True, max_length=10)

    def __init__(self, cog: "AttendanceCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            log_id = int(str(self.log_id).strip())
        except ValueError:
            await interaction.followup.send("That's not a valid number.", ephemeral=True)
            return
        confirmation = await self.cog._do_removelog(interaction.guild, log_id)
        await interaction.followup.send(confirmation, ephemeral=True)


class ExcludeModal(discord.ui.Modal, title="Exclude Player"):
    name = discord.ui.TextInput(label="Character name", required=True, max_length=32)

    def __init__(self, cog: "AttendanceCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmation = await self.cog._do_exclude(interaction.guild, str(self.name))
        await interaction.followup.send(confirmation, ephemeral=True)


class IncludeSelectView(discord.ui.View):
    """Short-lived (not persistent) - built fresh each time the Include
    button is clicked, populated with whoever is currently excluded."""

    def __init__(self, cog: "AttendanceCog", excluded_names: list):
        super().__init__(timeout=120)
        self.cog = cog

        select = discord.ui.Select(
            placeholder="Who's coming back?",
            options=[discord.SelectOption(label=name) for name in excluded_names[:25]],
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = self._select.values[0]
        confirmation = await self.cog._do_include(interaction.guild, name)
        await interaction.followup.send(confirmation, ephemeral=True)


# --- Persistent views (one template registered per view in cog_load) ------

class LogListView(discord.ui.View):
    def __init__(self, cog: "AttendanceCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Add Log", emoji="➕", style=discord.ButtonStyle.success, custom_id="attendance_addlog_btn")
    async def add_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage the log list.", ephemeral=True)
            return
        await interaction.response.send_modal(AddLogModal(self.cog))

    @discord.ui.button(label="Remove Log", emoji="➖", style=discord.ButtonStyle.danger, custom_id="attendance_removelog_btn")
    async def remove_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage the log list.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveLogModal(self.cog))


class RosterView(discord.ui.View):
    def __init__(self, cog: "AttendanceCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Refresh Roster", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="attendance_refresh_roster_btn")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can refresh the roster.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog._refresh_roster_message(interaction.guild)
        await interaction.followup.send("Roster refreshed.", ephemeral=True)


class ExcludedView(discord.ui.View):
    def __init__(self, cog: "AttendanceCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Exclude", emoji="🚫", style=discord.ButtonStyle.danger, custom_id="attendance_exclude_btn")
    async def exclude(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can exclude players.", ephemeral=True)
            return
        await interaction.response.send_modal(ExcludeModal(self.cog))

    @discord.ui.button(label="Include", emoji="✅", style=discord.ButtonStyle.success, custom_id="attendance_include_btn")
    async def include(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can re-include players.", ephemeral=True)
            return
        excluded = self.cog._get_excluded()
        if not excluded:
            await interaction.response.send_message("Nobody is currently excluded.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Who's coming back?", view=IncludeSelectView(self.cog, excluded), ephemeral=True
        )


class OverviewView(discord.ui.View):
    def __init__(self, cog: "AttendanceCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="attendance_refresh_overview_btn")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can refresh the overview.", ephemeral=True)
            return

        # Best-effort native cooldown via discord.py's own CooldownMapping -
        # falls open (allows the click) if anything about this doesn't
        # behave as expected, rather than blocking mods from a real /run.
        try:
            bucket = self.cog.refresh_cooldown.get_bucket(interaction)
            retry_after = bucket.update_rate_limit() if bucket else None
        except Exception:
            log.exception("Cooldown check failed - allowing the refresh through")
            retry_after = None

        if retry_after:
            await interaction.response.send_message(
                f"This button is cooling down - try again in {int(retry_after)}s, "
                "or use `/checkattendance run` (no cooldown there).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog._refresh_overview_message(interaction.guild)
        await interaction.followup.send("Overview refreshed.", ephemeral=True)


class AttendanceCog(commands.Cog):
    checkattendance_group = app_commands.Group(
        name="checkattendance", description="Raid attendance tracking (moderator only)"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.attendance_channel_id = int(os.environ["ATTENDANCE_CHANNEL_ID"])
        self.fresh_role_id = int(os.environ["FRESH_ROLE_ID"])
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.server_slug = os.environ["SERVER_SLUG"]
        self.server_region = os.environ.get("SERVER_REGION", "us")

        self.log_list_view = LogListView(self)
        self.roster_view = RosterView(self)
        self.excluded_view = ExcludedView(self)
        self.overview_view = OverviewView(self)

        # discord.py's own Cooldown machinery, reused here for a button
        # rather than a command - button-only, /checkattendance run below
        # never touches this.
        self.refresh_cooldown = commands.CooldownMapping.from_cooldown(
            1, float(REFRESH_COOLDOWN_SECONDS), commands.BucketType.guild
        )

    async def cog_load(self):
        self.bot.add_view(self.log_list_view)
        self.bot.add_view(self.roster_view)
        self.bot.add_view(self.excluded_view)
        self.bot.add_view(self.overview_view)

    # --- permission check (self-contained, same as other cogs) -----------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    # --- small storage helpers ---------------------------------------

    def _get_log_list(self) -> dict:
        record = self.bot.store.get(LOG_LIST_KEY)
        if record is None:
            record = {"pinned_message_id": None, "entries": []}
        return record

    def _save_log_list(self, record: dict):
        self.bot.store.set(LOG_LIST_KEY, **record)

    def _get_alt_links(self) -> dict:
        record = self.bot.store.get(ALT_LINKS_KEY)
        return (record or {}).get("links", {})

    def _save_alt_links(self, links: dict):
        self.bot.store.set(ALT_LINKS_KEY, links=links)

    def _get_main_overrides(self) -> dict:
        record = self.bot.store.get(MAIN_OVERRIDES_KEY)
        return (record or {}).get("overrides", {})

    def _save_main_overrides(self, overrides: dict):
        self.bot.store.set(MAIN_OVERRIDES_KEY, overrides=overrides)

    def _resolve_main_name(self, member: discord.Member) -> str:
        overrides = self._get_main_overrides()
        return overrides.get(str(member.id)) or member.display_name

    def _get_excluded(self) -> list:
        record = self.bot.store.get(EXCLUDED_KEY)
        return (record or {}).get("names", [])

    def _save_excluded(self, names: list):
        self.bot.store.set(EXCLUDED_KEY, names=names)

    def _get_baseline_overrides(self) -> dict:
        record = self.bot.store.get(BASELINE_KEY)
        return (record or {}).get("overrides", {})

    def _save_baseline_overrides(self, overrides: dict):
        self.bot.store.set(BASELINE_KEY, overrides=overrides)

    # --- class icon lookups (for the roster message) -------------------

    async def _lookup_class_by_name(self, character_name: str):
        try:
            char = await self.bot.wcl.get_character(character_name, self.server_slug, self.server_region)
        except Exception:
            log.exception("WCL class lookup failed for %s", character_name)
            return None
        return char["class_name"] if char else None

    async def _lookup_main_class(self, member: discord.Member, main_name: str):
        # Free path first: reuse the class already on file from their most
        # recent gear-check application, if the name still matches.
        result = self.bot.store.find_latest_by_applicant(member.id)
        if result:
            _, record = result
            if record.get("character_name", "").lower() == main_name.lower() and record.get("class_name"):
                return record["class_name"]
        return await self._lookup_class_by_name(main_name)

    async def _get_recent_attendance_union(self, window_size: int) -> set:
        """Lowercase names present (per ATTENDANCE_MIN_KILLS_PER_LOG) in ANY
        of the most recent `window_size` tagged logs - used to filter the
        Fresh-member list on the roster down to people who've actually
        shown up recently, rather than every Fresh member ever approved."""
        record = self._get_log_list()
        window_entries = record["entries"][-window_size:]
        union = set()
        for entry in window_entries:
            try:
                names = await self.bot.wcl.get_report_attendance(
                    entry["report_code"], min_kills=config.ATTENDANCE_MIN_KILLS_PER_LOG
                )
            except Exception:
                log.exception("Failed to fetch attendance for report %s", entry["report_code"])
                names = set()
            union |= {n.lower() for n in names}
        return union

    # --- pinned log-list message -------------------------------------

    def _render_log_list_embed(self, entries: list) -> discord.Embed:
        embed = discord.Embed(
            title="📋 Main Raid Log List",
            description=(
                f"Add/remove with the buttons below, or `/checkattendance addlog` / "
                f"`removelog id:<N>`.\nThe most recent **{config.ATTENDANCE_WINDOW}** "
                f"entries (marked 🔸) are what the overview currently uses."
            ),
            color=discord.Color.blurple(),
        )
        if not entries:
            embed.add_field(name="No logs yet", value="Add one to get started.", inline=False)
        else:
            in_window_ids = {e["id"] for e in entries[-config.ATTENDANCE_WINDOW:]}
            lines = []
            for entry in reversed(entries):
                marker = "🔸" if entry["id"] in in_window_ids else "▫️"
                lines.append(
                    f"{marker} **#{entry['id']}** — {entry['date']} · {entry['tier']} · "
                    f"[Log](https://fresh.warcraftlogs.com/reports/{entry['report_code']})"
                )
            value = "\n".join(lines)
            if len(value) > 4000:
                value = value[:4000] + "\n… (older entries truncated - still counted, just not shown)"
            embed.add_field(name="Entries", value=value, inline=False)
        embed.set_footer(text=LOG_LIST_MARKER)
        return embed

    async def _sync_log_list_message(self, guild):
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            log.warning("ATTENDANCE_CHANNEL_ID set but channel not found/visible")
            return

        record = self._get_log_list()
        embed = self._render_log_list_embed(record["entries"])

        if record.get("pinned_message_id"):
            try:
                message = await channel.fetch_message(record["pinned_message_id"])
                await message.edit(embed=embed, view=self.log_list_view)
                return
            except (discord.NotFound, discord.Forbidden):
                pass

        try:
            message = await channel.send(embed=embed, view=self.log_list_view)
            await message.pin()
            record["pinned_message_id"] = message.id
            self._save_log_list(record)
        except Exception:
            log.exception("Failed to post/pin the attendance log-list message")

    # --- roster message ------------------------------------------------

    async def _build_roster_embed(self, guild: discord.Guild) -> discord.Embed:
        fresh_role = guild.get_role(self.fresh_role_id)
        regular_role = guild.get_role(config.REGULAR_ROLE_ID)
        alt_links = self._get_alt_links()
        recent_activity = await self._get_recent_attendance_union(config.ROSTER_FRESH_ACTIVITY_WINDOW)

        lines = []
        skipped_inactive_fresh = 0
        members = sorted(
            (m for m in guild.members if not m.bot), key=lambda m: m.display_name.lower()
        )
        for member in members:
            if any(r.id == self.mod_role_id for r in member.roles):
                continue
            has_fresh = fresh_role in member.roles if fresh_role else False
            has_regular = regular_role in member.roles if regular_role else False
            if not has_fresh and not has_regular:
                continue

            main_name = self._resolve_main_name(member)
            main_lower = main_name.lower()
            alts = [alt for alt, main in alt_links.items() if main.lower() == main_lower]

            # Fresh-only members (not also Regular) are hidden from this
            # list unless they've shown up in at least one of the last
            # ROSTER_FRESH_ACTIVITY_WINDOW logs - Regular members are
            # always shown regardless, since that list stays small/curated.
            if has_fresh and not has_regular:
                relevant_names = {main_lower} | set(alts)
                if not (relevant_names & recent_activity):
                    skipped_inactive_fresh += 1
                    continue

            main_class = await self._lookup_main_class(member, main_name)
            main_icon = icons.resolve_class_icon(guild, main_class) if main_class else ""

            line = f"{member.mention} → {main_icon} **{main_name}**".replace("  ", " ")
            for alt in alts:
                alt_class = await self._lookup_class_by_name(alt)
                alt_icon = icons.resolve_class_icon(guild, alt_class) if alt_class else ""
                line += f"\n\u2003↳ {alt_icon} {alt}".replace("  ", " ")
                await asyncio.sleep(0.1)
            lines.append(line)

        embed = discord.Embed(
            title="🧾 Raider Roster",
            description=(
                "Discord member → main character (alts indented below). Regular members "
                "always shown; Fresh members only shown if active in at least one of the "
                f"last {config.ROSTER_FRESH_ACTIVITY_WINDOW} logs"
                + (f" - {skipped_inactive_fresh} inactive Fresh member(s) hidden." if skipped_inactive_fresh else ".")
            ),
            color=discord.Color.blurple(),
        )
        value = "\n".join(lines) if lines else "No active Fresh/Regular members found."
        if len(value) > 4000:
            value = value[:4000] + "\n… (truncated)"
        embed.add_field(name="Raiders", value=value, inline=False)
        embed.set_footer(text=ROSTER_MARKER)
        return embed

    async def _refresh_roster_message(self, guild: discord.Guild):
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            return
        embed = await self._build_roster_embed(guild)

        record = self.bot.store.get(ROSTER_MESSAGE_KEY)
        if record and record.get("message_id"):
            try:
                message = await channel.fetch_message(record["message_id"])
                await message.edit(embed=embed, view=self.roster_view)
                return
            except (discord.NotFound, discord.Forbidden):
                pass

        try:
            message = await channel.send(embed=embed, view=self.roster_view)
            await message.pin()
            self.bot.store.set(ROSTER_MESSAGE_KEY, message_id=message.id)
        except Exception:
            log.exception("Failed to post/pin the attendance roster message")

    # --- excluded-players message ---------------------------------------

    def _render_excluded_embed(self) -> discord.Embed:
        excluded = self._get_excluded()
        embed = discord.Embed(
            title="🚫 Excused Players",
            description="Excused players are skipped entirely by the attendance overview.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Currently excused", value=", ".join(excluded) if excluded else "None", inline=False)
        embed.set_footer(text=EXCLUDED_MARKER)
        return embed

    async def _refresh_excluded_message(self, guild):
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            return
        embed = self._render_excluded_embed()

        record = self.bot.store.get(EXCLUDED_MESSAGE_KEY)
        if record and record.get("message_id"):
            try:
                message = await channel.fetch_message(record["message_id"])
                await message.edit(embed=embed, view=self.excluded_view)
                return
            except (discord.NotFound, discord.Forbidden):
                pass

        try:
            message = await channel.send(embed=embed, view=self.excluded_view)
            await message.pin()
            self.bot.store.set(EXCLUDED_MESSAGE_KEY, message_id=message.id)
        except Exception:
            log.exception("Failed to post/pin the attendance excluded-players message")

    # --- explainer -------------------------------------------------------

    async def _ensure_explainer_message(self):
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            log.warning("ATTENDANCE_CHANNEL_ID set but channel not found/visible")
            return

        try:
            pins = await channel.pins()
        except discord.Forbidden:
            log.warning("Missing permission to read pins in attendance channel")
            return

        if any(m.embeds and m.embeds[0].footer.text == EXPLAINER_MARKER for m in pins):
            return

        embed = discord.Embed(
            title="How attendance tracking works",
            description=(
                "**The short version:** to be Regular-eligible, someone needs to have "
                f"attended (killed at least {config.ATTENDANCE_MIN_KILLS_PER_LOG} boss) in "
                f"at least {config.ATTENDANCE_MIN_ATTENDED} of the last "
                f"{config.ATTENDANCE_WINDOW} tagged main raids.\n\n"

                "**📋 Main Raid Log List** - the list of raids that actually count. "
                "Only logs added here are used - nothing is auto-detected from "
                "WarcraftLogs, so a random alt run or an off-night log never sneaks in. "
                "Use the ➕/➖ buttons or `/checkattendance addlog` / `removelog`.\n\n"

                "**🧾 Raider Roster** - shows every Fresh/Regular member, their main "
                "character (with class icon), and any linked alts. If someone's Discord "
                "nickname doesn't match their character name, fix it with "
                "`/checkattendance setmain`. Alts are added with `/checkattendance link`.\n\n"

                "**🚫 Excused Players** - players sitting out attendance tracking "
                "temporarily (injury, break, etc.) - they're skipped entirely rather than "
                "counted as absent. Use the buttons or `/checkattendance exclude`/`include`.\n\n"

                "**📊 Attendance Overview** - the actual promotion/demotion discussion "
                "aid. Click Refresh (or run `/checkattendance run`, which has no cooldown) "
                "to recompute it against the current log list.\n\n"

                "Every button here has a matching slash command too, if you'd rather type "
                "than click - `/checkattendance` to see them all."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=EXPLAINER_MARKER)

        try:
            message = await channel.send(embed=embed)
            await message.pin()
            self.bot.store.set(EXPLAINER_MESSAGE_KEY, message_id=message.id)
        except Exception:
            log.exception("Failed to post/pin the attendance explainer")

    # --- startup: ensure all four functional messages + explainer -------

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_explainer_message()
        await self._sync_log_list_message(None)
        guild_id = os.environ.get("DISCORD_GUILD_ID")
        guild = self.bot.get_guild(int(guild_id)) if guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is not None:
            if not (self.bot.store.get(ROSTER_MESSAGE_KEY) or {}).get("message_id"):
                await self._refresh_roster_message(guild)
            if not (self.bot.store.get(EXCLUDED_MESSAGE_KEY) or {}).get("message_id"):
                await self._refresh_excluded_message(guild)
            if not (self.bot.store.get(OVERVIEW_MESSAGE_KEY) or {}).get("message_id"):
                await self._refresh_overview_message(guild)

    # --- shared action logic (used by both slash commands and buttons) --

    async def _do_addlog(self, guild, date: str, tier: str, link: str) -> str:
        report_code = _extract_report_code(link)
        record = self._get_log_list()
        next_id = (max((e["id"] for e in record["entries"]), default=0)) + 1
        record["entries"].append({
            "id": next_id, "date": date.strip(), "tier": tier.strip(), "report_code": report_code,
        })
        self._save_log_list(record)
        await self._sync_log_list_message(guild)
        return f"Added log #{next_id} ({date.strip()} · {tier.strip()})."

    async def _do_removelog(self, guild, log_id: int) -> str:
        record = self._get_log_list()
        before = len(record["entries"])
        record["entries"] = [e for e in record["entries"] if e["id"] != log_id]
        if len(record["entries"]) == before:
            return f"No log with ID #{log_id} found."
        self._save_log_list(record)
        await self._sync_log_list_message(guild)
        return f"Removed log #{log_id}."

    async def _do_exclude(self, guild, name: str) -> str:
        excluded = self._get_excluded()
        name = name.strip()
        if not any(n.lower() == name.lower() for n in excluded):
            excluded.append(name)
            self._save_excluded(excluded)
        await self._refresh_excluded_message(guild)
        return f"**{name}** is now excused from attendance tracking."

    async def _do_include(self, guild, name: str) -> str:
        name = name.strip()
        excluded = [n for n in self._get_excluded() if n.lower() != name.lower()]
        self._save_excluded(excluded)

        overrides = self._get_baseline_overrides()
        overrides[name.lower()] = config.ATTENDANCE_INCLUDE_BASELINE
        self._save_baseline_overrides(overrides)

        await self._refresh_excluded_message(guild)
        return (
            f"**{name}** is back on attendance tracking - assumed "
            f"{config.ATTENDANCE_INCLUDE_BASELINE}/{config.ATTENDANCE_WINDOW} for the next "
            "`/checkattendance run` (or Refresh click) only, then real log data takes over again."
        )

    # --- attendance computation + overview message -----------------------

    async def _compute_attendance(self, guild: discord.Guild) -> dict:
        record = self._get_log_list()
        window_entries = record["entries"][-config.ATTENDANCE_WINDOW:]

        rosters = []
        for entry in window_entries:
            try:
                names = await self.bot.wcl.get_report_attendance(
                    entry["report_code"], min_kills=config.ATTENDANCE_MIN_KILLS_PER_LOG
                )
            except Exception:
                log.exception("Failed to fetch attendance for report %s", entry["report_code"])
                names = set()
            rosters.append({n.lower() for n in names})

        alt_links = self._get_alt_links()
        excluded = {n.lower() for n in self._get_excluded()}
        baseline_overrides = self._get_baseline_overrides()

        fresh_role = guild.get_role(self.fresh_role_id)
        regular_role = guild.get_role(config.REGULAR_ROLE_ID)

        promote, demote, watch_promote, watch_demote = [], [], [], []
        excluded_display = []
        window_size = len(rosters)

        for member in guild.members:
            if member.bot:
                continue
            if any(r.id == self.mod_role_id for r in member.roles):
                continue

            has_fresh = fresh_role in member.roles if fresh_role else False
            has_regular = regular_role in member.roles if regular_role else False
            if not has_fresh and not has_regular:
                continue

            main_name = self._resolve_main_name(member)
            main_lower = main_name.lower()

            if main_lower in excluded:
                excluded_display.append(main_name)
                continue

            relevant_names = {main_lower} | {
                alt for alt, main in alt_links.items() if main.lower() == main_lower
            }

            if main_lower in baseline_overrides:
                attended_count = baseline_overrides[main_lower]
            else:
                attended_count = sum(1 for roster in rosters if relevant_names & roster)

            without_oldest = (
                sum(1 for roster in rosters[1:] if relevant_names & roster) if rosters else 0
            )
            meets_threshold = attended_count >= config.ATTENDANCE_MIN_ATTENDED

            if has_fresh and not has_regular:
                if meets_threshold:
                    promote.append((main_name, attended_count, window_size))
                elif without_oldest + 1 >= config.ATTENDANCE_MIN_ATTENDED:
                    watch_promote.append((main_name, attended_count, window_size))
            elif has_regular:
                if not meets_threshold:
                    demote.append((main_name, attended_count, window_size))
                elif without_oldest < config.ATTENDANCE_MIN_ATTENDED:
                    watch_demote.append((main_name, attended_count, window_size))

        if baseline_overrides:
            self._save_baseline_overrides({})

        return {
            "promote": promote, "demote": demote,
            "watch_promote": watch_promote, "watch_demote": watch_demote,
            "excluded": excluded_display, "window_size": window_size,
        }

    def _render_overview_embed(self, results: dict) -> discord.Embed:
        embed = discord.Embed(
            title="📊 Attendance Overview",
            description=(
                f"Based on the {results['window_size']} most recent tagged main-raid log(s) "
                f"(need {config.ATTENDANCE_MIN_ATTENDED}/{config.ATTENDANCE_WINDOW} to be Regular-eligible)."
            ),
            color=discord.Color.blurple(),
        )

        def _fmt(rows):
            return "\n".join(f"**{name}** — {count}/{window}" for name, count, window in rows) or "None"

        embed.add_field(name="✅ Promotion eligible (Fresh → Regular)", value=_fmt(results["promote"]), inline=False)
        embed.add_field(name="⚠️ Demotion review (Regular below threshold)", value=_fmt(results["demote"]), inline=False)
        embed.add_field(name="👀 On watch for next raid", value=_fmt(results["watch_promote"] + results["watch_demote"]), inline=False)
        embed.add_field(name="🚫 Excluded", value=", ".join(results["excluded"]) or "None", inline=False)
        embed.set_footer(text="Role changes are manual - this is a discussion aid, not an automatic action.")
        return embed

    async def _refresh_overview_message(self, guild: discord.Guild):
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            return

        results = await self._compute_attendance(guild)
        embed = self._render_overview_embed(results)

        record = self.bot.store.get(OVERVIEW_MESSAGE_KEY)
        if record and record.get("message_id"):
            try:
                message = await channel.fetch_message(record["message_id"])
                await message.edit(embed=embed, view=self.overview_view)
                return
            except (discord.NotFound, discord.Forbidden):
                pass

        try:
            message = await channel.send(embed=embed, view=self.overview_view)
            self.bot.store.set(OVERVIEW_MESSAGE_KEY, message_id=message.id)
        except Exception:
            log.exception("Failed to post the attendance overview message")

    # --- slash commands (same actions as the buttons above) --------------

    @checkattendance_group.command(name="addlog", description="Add a main-raid log to the tracked list")
    @app_commands.describe(date="Date of the raid, e.g. 2026-07-29", tier="Tier/zone, e.g. SSC/TK", link="WCL report link (or bare report code)")
    async def addlog(self, interaction: discord.Interaction, date: str, tier: str, link: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage the log list.", ephemeral=True)
            return
        confirmation = await self._do_addlog(interaction.guild, date, tier, link)
        await interaction.response.send_message(confirmation, ephemeral=True)

    @checkattendance_group.command(name="removelog", description="Remove a log from the tracked list by its ID")
    @app_commands.describe(id="The #ID shown next to the log in the pinned list")
    async def removelog(self, interaction: discord.Interaction, id: int):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage the log list.", ephemeral=True)
            return
        confirmation = await self._do_removelog(interaction.guild, id)
        await interaction.response.send_message(confirmation, ephemeral=True)

    @checkattendance_group.command(name="link", description="Link an alt character to a main, for attendance purposes")
    @app_commands.describe(main="The main character's name", alt="The alt character's name")
    async def link(self, interaction: discord.Interaction, main: str, alt: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can link characters.", ephemeral=True)
            return
        links = self._get_alt_links()
        links[alt.strip().lower()] = main.strip()
        self._save_alt_links(links)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._refresh_roster_message(interaction.guild)
        await interaction.followup.send(f"Linked **{alt.strip()}** as an alt of **{main.strip()}**.", ephemeral=True)

    @checkattendance_group.command(name="removealt", description="Remove a previously linked alt")
    @app_commands.describe(alt="The alt character's name")
    async def removealt(self, interaction: discord.Interaction, alt: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage links.", ephemeral=True)
            return
        links = self._get_alt_links()
        key = alt.strip().lower()
        if key not in links:
            await interaction.response.send_message(f"No link found for **{alt.strip()}**.", ephemeral=True)
            return
        removed_main = links.pop(key)
        self._save_alt_links(links)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._refresh_roster_message(interaction.guild)
        await interaction.followup.send(f"Removed **{alt.strip()}** as an alt of **{removed_main}**.", ephemeral=True)

    @checkattendance_group.command(name="setmain", description="Override a member's main character name")
    @app_commands.describe(member="The Discord member", character="Their actual main character name")
    async def setmain(self, interaction: discord.Interaction, member: discord.Member, character: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can set main-name overrides.", ephemeral=True)
            return
        overrides = self._get_main_overrides()
        overrides[str(member.id)] = character.strip()
        self._save_main_overrides(overrides)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._refresh_roster_message(interaction.guild)
        await interaction.followup.send(
            f"{member.mention}'s main character is now set to **{character.strip()}**.", ephemeral=True
        )

    @checkattendance_group.command(name="removemain", description="Remove a member's main-name override")
    @app_commands.describe(member="The Discord member")
    async def removemain(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage main-name overrides.", ephemeral=True)
            return
        overrides = self._get_main_overrides()
        if str(member.id) not in overrides:
            await interaction.response.send_message(f"{member.mention} has no main-name override set.", ephemeral=True)
            return
        del overrides[str(member.id)]
        self._save_main_overrides(overrides)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._refresh_roster_message(interaction.guild)
        await interaction.followup.send(f"Removed {member.mention}'s main-name override.", ephemeral=True)

    @checkattendance_group.command(name="links", description="Debug: show a member's resolved main name and linked alts")
    @app_commands.describe(member="The Discord member")
    async def links(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can view links.", ephemeral=True)
            return
        overrides = self._get_main_overrides()
        has_override = str(member.id) in overrides
        main_name = self._resolve_main_name(member)
        alt_links = self._get_alt_links()
        alts = [alt for alt, main in alt_links.items() if main.lower() == main_name.lower()]
        lines = [
            f"**Resolved main name:** {main_name} "
            f"({'override set via /checkattendance setmain' if has_override else 'from Discord nickname'})",
            f"**Linked alts:** {', '.join(alts) if alts else 'None'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @checkattendance_group.command(name="exclude", description="Excuse a player from attendance tracking")
    @app_commands.describe(name="Their main character's name")
    async def exclude(self, interaction: discord.Interaction, name: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can exclude players.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmation = await self._do_exclude(interaction.guild, name)
        await interaction.followup.send(confirmation, ephemeral=True)

    @checkattendance_group.command(name="include", description="Resume attendance tracking for a previously excused player")
    @app_commands.describe(name="Their main character's name")
    async def include(self, interaction: discord.Interaction, name: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can re-include players.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmation = await self._do_include(interaction.guild, name)
        await interaction.followup.send(confirmation, ephemeral=True)

    @checkattendance_group.command(name="run", description="Generate/refresh the attendance overview (no cooldown)")
    async def run(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can run attendance checks.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._refresh_overview_message(interaction.guild)
        await interaction.followup.send("Overview posted/refreshed.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AttendanceCog(bot))