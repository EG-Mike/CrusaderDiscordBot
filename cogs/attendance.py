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
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import config
import icons

log = logging.getLogger("wow-apply-bot.attendance")

# Same guild-local timezone convention already used in cogs/raid_summary.py
# (AMSTERDAM_TZ there) - "last updated" timestamps are far more readable in
# local time than UTC.
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

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
EXPLAINER_MESSAGE_KEY = "attendance_explainer_message"

LOG_LIST_MARKER = "attendance-log-list"
ROSTER_MARKER = "attendance-roster"
EXPLAINER_MARKER = "attendance-explainer"

REPORT_LINK_RE = re.compile(r"(?:reports/|^)([A-Za-z0-9]{8,20})(?:[/#].*)?$")

REFRESH_COOLDOWN_SECONDS = 300  # 5 minutes - button only, /checkattendance run is never limited


def _chunk_lines_for_embed_fields(lines: list, max_field_len: int = 1024, max_total_len: int = 5000):
    """
    Splits a list of text lines into embed-field-safe chunks. Discord caps a
    single field's value at 1024 chars (hard limit - this is what was
    actually being violated before, not the ~4000 char limit that used to
    be checked here by mistake) AND caps the whole embed's combined content
    at 6000 chars. Returns (chunks, truncated) - chunks is a list of
    already-joined strings each under max_field_len, truncated is True if
    some lines had to be dropped to stay under max_total_len.
    """
    chunks = []
    current = []
    current_len = 0
    total_len = 0
    truncated = False

    for line in lines:
        line_len = len(line) + 1  # +1 for the joining newline
        if total_len + line_len > max_total_len:
            truncated = True
            break
        if current and current_len + line_len > max_field_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
        total_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks, truncated


_NAME_TAG_RE = re.compile(r"^<[^>]+>\s*")


def _strip_name_tag(name: str) -> str:
    """Strips a leading <TAG> prefix (e.g. "<R> Moffel" -> "Moffel") - some
    guilds prefix Regular nicknames this way, which otherwise breaks name
    matching against WCL rosters (real character names never contain
    literal < or > characters, so this is always safe to strip)."""
    return _NAME_TAG_RE.sub("", name).strip()


def _extract_report_code(link: str) -> str:
    """Accepts a bare report code or a full WCL report URL."""
    link = link.strip().rstrip("/")
    match = REPORT_LINK_RE.search(link)
    return match.group(1) if match else link


def _footer_with_update_stamp(marker: str, updated_by: str = None) -> str:
    """
    Appends a "Last updated by X - <date> <time>" line to a message's footer
    marker text, so mods can see at a glance whether the log
    list/roster/overview reflects the latest data without having to ask -
    see the module docstring. Embed footers render as plain text (no
    markdown/mentions), so `updated_by` must already be a plain display
    name/label by the time it gets here, never a <@id> mention. `updated_by`
    of None (e.g. the startup reconciliation pass, which has no acting user)
    just posts the marker alone, same as before this existed.
    """
    if not updated_by:
        return marker
    when = datetime.now(timezone.utc).astimezone(AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M")
    return f"{marker} · Last updated by {updated_by} · {when}"


# --- Modals ---------------------------------------------------------------

class AddLogModal(discord.ui.Modal, title="Add Main Raid Log"):
    date = discord.ui.TextInput(label="Date (e.g. 2026-07-29)", required=True, max_length=32)
    tier = discord.ui.TextInput(label="Tier/zone (e.g. SSC/TK)", required=True, max_length=64)
    link = discord.ui.TextInput(label="WCL report link or code", required=True, max_length=200)

    def __init__(self, cog: "AttendanceCog", prefill_date: str = None, prefill_tier: str = None,
                 prefill_link: str = None):
        super().__init__()
        self.cog = cog
        # Pre-filled (but always still editable) when opened from
        # AddLogPickerView's #logs dropdown - see LogListView.add_log.
        if prefill_date:
            self.date.default = prefill_date
        if prefill_tier:
            self.tier.default = prefill_tier
        if prefill_link:
            self.link.default = prefill_link

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmation = await self.cog._do_addlog(
            interaction.guild, str(self.date), str(self.tier), str(self.link),
            updated_by=interaction.user.display_name,
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
        confirmation = await self.cog._do_removelog(
            interaction.guild, log_id, updated_by=interaction.user.display_name
        )
        await interaction.followup.send(confirmation, ephemeral=True)


class ExcludeModal(discord.ui.Modal, title="Exclude Player"):
    name = discord.ui.TextInput(label="Character name (exact)", required=True, max_length=32)
    reason = discord.ui.TextInput(label="Reason (e.g. vacation, parental leave)", required=True, max_length=200)

    def __init__(self, cog: "AttendanceCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        typed_name = str(self.name).strip()

        # No autocomplete is possible in a modal (hard Discord platform
        # limit, not something buildable around) - so this validates
        # against the live roster after the fact instead, and offers close
        # matches if it's not exact.
        candidates = self.cog._get_roster_candidate_names(interaction.guild)
        exact = next((c for c in candidates if c.lower() == typed_name.lower()), None)
        if exact is None:
            close = [c for c in candidates if typed_name.lower() in c.lower()][:5]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            await interaction.followup.send(
                f"Couldn't find **{typed_name}** on the current roster.{hint} Please try "
                "again with the exact name - or use `/checkattendance exclude` for live "
                "suggestions as you type.",
                ephemeral=True,
            )
            return

        confirmation = await self.cog._do_exclude(interaction.guild, exact, str(self.reason).strip())
        await interaction.followup.send(confirmation, ephemeral=True)


class RemoveExcludedModal(discord.ui.Modal, title="Remove Excused Player"):
    excluded_id = discord.ui.TextInput(label="Excused-player #ID to remove", required=True, max_length=10)

    def __init__(self, cog: "AttendanceCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            excluded_id = int(str(self.excluded_id).strip())
        except ValueError:
            await interaction.followup.send("That's not a valid number.", ephemeral=True)
            return
        confirmation = await self.cog._do_remove_excluded(interaction.guild, excluded_id)
        await interaction.followup.send(confirmation, ephemeral=True)


# --- Ephemeral picker (not persistent - same short-lived-view pattern as
# raid_summary.py's RaidSummaryOptionsView) --------------------------------

class AddLogPickerView(discord.ui.View):
    """
    Shown by LogListView's Add Log button instead of going straight to
    AddLogModal, when cogs/raid_logs.py is loaded and has recent tagged/
    reposted logs to offer - the same "pick from a dropdown instead of
    hunting down and pasting a link" upgrade /raidsummary already has (see
    RaidLogsCog.get_recent_entries_for_picker). Picking one still opens
    AddLogModal for confirmation - date/tier/link just arrive pre-filled
    (and stay fully editable) instead of blank.
    """
    def __init__(self, cog: "AttendanceCog", entries: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.report_code = None
        self._entries_by_code = {e["report_code"]: e for e in entries}

        self.select = discord.ui.Select(
            placeholder="Pick a recent log from #logs",
            options=[
                discord.SelectOption(
                    label=e["label"][:100], value=e["report_code"], description=(e.get("description") or "")[:100],
                )
                for e in entries[:25]
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.continue_button = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary, disabled=True)
        self.continue_button.callback = self._on_continue
        self.add_item(self.continue_button)

        self.manual_button = discord.ui.Button(label="Not listed - enter manually", style=discord.ButtonStyle.secondary)
        self.manual_button.callback = self._on_manual
        self.add_item(self.manual_button)

    async def _on_select(self, interaction: discord.Interaction):
        self.report_code = self.select.values[0]
        self.continue_button.disabled = False
        for opt in self.select.options:
            opt.default = opt.value == self.report_code
        await interaction.response.edit_message(view=self)

    async def _on_continue(self, interaction: discord.Interaction):
        entry = self._entries_by_code.get(self.report_code, {})
        modal = AddLogModal(
            self.cog, prefill_date=entry.get("date_guess"),
            prefill_tier=entry.get("description"), prefill_link=self.report_code,
        )
        await interaction.response.send_modal(modal)
        self.stop()

    async def _on_manual(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddLogModal(self.cog))
        self.stop()


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

        raid_logs_cog = self.cog.bot.get_cog("RaidLogsCog")
        entries = raid_logs_cog.get_recent_entries_for_picker() if raid_logs_cog else []
        if entries:
            await interaction.response.send_message(
                "Pick the log to add (or enter one manually if it's not listed):",
                view=AddLogPickerView(self.cog, entries), ephemeral=True,
            )
        else:
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
        await self.cog._refresh_roster_message(interaction.guild, updated_by=interaction.user.display_name)
        await interaction.followup.send("Roster refreshed.", ephemeral=True)

    @discord.ui.button(label="+ Add player", style=discord.ButtonStyle.success, custom_id="attendance_exclude_btn")
    async def add_excluded(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can exclude players.", ephemeral=True)
            return
        await interaction.response.send_modal(ExcludeModal(self.cog))

    @discord.ui.button(label="- Remove player", style=discord.ButtonStyle.danger, custom_id="attendance_include_btn")
    async def remove_excluded(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can remove excused players.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveExcludedModal(self.cog))


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
        await self.cog._refresh_overview_message(interaction.guild, updated_by=interaction.user.display_name)
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

        # Optional - every overview refresh that has a real acting user/
        # label (button click, /checkattendance run, or cogs/raid_logs.py's
        # Summarize automation - never the silent startup reconciliation
        # pass) also mirrors the overview into this channel, since
        # #attendance-check itself is easy to miss unless a mod happens to
        # already be looking at it. See _refresh_overview_message.
        moderator_channel_id = os.environ.get("MODERATOR_CHANNEL_ID")
        self.moderator_channel_id = int(moderator_channel_id) if moderator_channel_id else None

        self.log_list_view = LogListView(self)
        self.roster_view = RosterView(self)
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

    def get_alt_links(self) -> dict:
        """
        Public accessor (cross-cog - see cogs/tier_retrospective.py's
        attendance calculation) for the same {alt_name_lowercased:
        main_name} map _get_alt_links() reads, so another cog can resolve
        a WCL character name to its main for attendance purposes without
        reaching into this cog's "private" method or duplicating
        ALT_LINKS_KEY's store key itself.
        """
        return self._get_alt_links()

    def _save_alt_links(self, links: dict):
        self.bot.store.set(ALT_LINKS_KEY, links=links)

    def _get_main_overrides(self) -> dict:
        record = self.bot.store.get(MAIN_OVERRIDES_KEY)
        return (record or {}).get("overrides", {})

    def _save_main_overrides(self, overrides: dict):
        self.bot.store.set(MAIN_OVERRIDES_KEY, overrides=overrides)

    def _resolve_main_name(self, member: discord.Member) -> str:
        overrides = self._get_main_overrides()
        raw = overrides.get(str(member.id)) or member.display_name
        return _strip_name_tag(raw)

    def _get_excluded_entries(self) -> list:
        record = self.bot.store.get(EXCLUDED_KEY)
        return (record or {}).get("entries", [])

    def _save_excluded_entries(self, entries: list):
        self.bot.store.set(EXCLUDED_KEY, entries=entries)

    def _get_excluded_names(self) -> set:
        return {e["name"].lower() for e in self._get_excluded_entries()}

    def _get_roster_candidate_names(self, guild: discord.Guild) -> list:
        """Current Fresh/Regular (non-mod) members not already excused -
        used both for the Add-player dropdown and the exclude command's
        autocomplete. Pure local lookups, no I/O, so it's fast enough for
        autocomplete's tight response window."""
        fresh_role = guild.get_role(self.fresh_role_id)
        regular_role = guild.get_role(config.REGULAR_ROLE_ID)
        excluded_names = self._get_excluded_names()

        names = []
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
            if main_name.lower() not in excluded_names:
                names.append(main_name)

        return sorted(set(names), key=str.lower)

    def _get_baseline_overrides(self) -> dict:
        record = self.bot.store.get(BASELINE_KEY)
        return (record or {}).get("overrides", {})

    def _save_baseline_overrides(self, overrides: dict):
        self.bot.store.set(BASELINE_KEY, overrides=overrides)

    # --- class icon lookups (for the roster message) -------------------

    async def _lookup_class_by_name(self, character_name: str, cache: dict = None):
        key = character_name.lower()
        if cache is not None and key in cache:
            return cache[key]
        try:
            char = await self.bot.wcl.get_character(character_name, self.server_slug, self.server_region)
        except Exception:
            log.exception("WCL class lookup failed for %s", character_name)
            char = None
        result = char["class_name"] if char else None
        if cache is not None:
            cache[key] = result
        await asyncio.sleep(0.2)  # pacing - this loop can otherwise burst dozens of
                                   # requests back-to-back with zero delay, which is
                                   # exactly what trips WCL's 429 rate limiting
        return result

    async def _lookup_main_class(self, member: discord.Member, main_name: str, cache: dict = None):
        # Free path first: reuse the class already on file from their most
        # recent gear-check application, if the name still matches - no
        # network call, no pacing needed.
        result = self.bot.store.find_latest_by_applicant(member.id)
        if result:
            _, record = result
            if record.get("character_name", "").lower() == main_name.lower() and record.get("class_name"):
                return record["class_name"]
        return await self._lookup_class_by_name(main_name, cache=cache)

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
            await asyncio.sleep(0.2)  # pacing between reports, not just within one report's fights
        return union

    # --- pinned log-list message -------------------------------------

    def _render_log_list_embed(self, entries: list, updated_by: str = None) -> discord.Embed:
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
            chunks, truncated = _chunk_lines_for_embed_fields(lines)
            for i, chunk in enumerate(chunks[:24]):
                field_name = "Entries" if i == 0 else "\u200b"
                embed.add_field(name=field_name, value=chunk, inline=False)
            if truncated or len(chunks) > 24:
                embed.add_field(
                    name="\u200b",
                    value="… older entries truncated - still counted, just not all shown here.",
                    inline=False,
                )
        embed.set_footer(text=_footer_with_update_stamp(LOG_LIST_MARKER, updated_by))
        return embed

    async def _sync_log_list_message(self, guild, updated_by: str = None):
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            log.warning("ATTENDANCE_CHANNEL_ID set but channel not found/visible")
            return

        record = self._get_log_list()
        embed = self._render_log_list_embed(record["entries"], updated_by=updated_by)

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

    async def _build_roster_embed(self, guild: discord.Guild, updated_by: str = None) -> discord.Embed:
        fresh_role = guild.get_role(self.fresh_role_id)
        regular_role = guild.get_role(config.REGULAR_ROLE_ID)
        alt_links = self._get_alt_links()
        recent_activity = await self._get_recent_attendance_union(config.ROSTER_FRESH_ACTIVITY_WINDOW)
        class_cache = {}  # shared for this whole refresh - main/alt/excused names can overlap

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

            main_class = await self._lookup_main_class(member, main_name, cache=class_cache)
            main_icon = icons.resolve_class_icon(guild, main_class) if main_class else ""

            line = f"{member.mention} → {main_icon} **{main_name}**".replace("  ", " ")
            for alt in alts:
                alt_class = await self._lookup_class_by_name(alt, cache=class_cache)
                alt_icon = icons.resolve_class_icon(guild, alt_class) if alt_class else ""
                line += f"\n\u2003↳ {alt_icon} {alt}".replace("  ", " ")
            lines.append(line)

        embed = discord.Embed(
            title="🧾 Raider Roster",
            description=(
                "Discord nickname → main character (alts indented below). Regular members "
                "always shown; Fresh members only shown if active in at least one of the "
                f"last '{config.ROSTER_FRESH_ACTIVITY_WINDOW}' logs"
                + (f" - '{skipped_inactive_fresh}' inactive Fresh member(s) hidden." if skipped_inactive_fresh else ".")
            ),
            color=discord.Color.blurple(),
        )

        # Conservative budgets here, not the chunker's own defaults - this
        # embed has TWO sections (raiders + excused) sharing Discord's one
        # 25-field / ~6000-char total budget, so each needs headroom left
        # for the other rather than each independently maxing out.
        if not lines:
            embed.add_field(name="Raiders", value="No active Fresh/Regular members found.", inline=False)
        else:
            chunks, truncated = _chunk_lines_for_embed_fields(lines, max_total_len=4000)
            for i, chunk in enumerate(chunks[:18]):
                field_name = "Raiders" if i == 0 else "\u200b"
                embed.add_field(name=field_name, value=chunk, inline=False)
            if truncated or len(chunks) > 18:
                embed.add_field(
                    name="\u200b",
                    value="… raider list truncated - too many to fit in one message. "
                          "Consider lowering ROSTER_FRESH_ACTIVITY_WINDOW in config/deployment.py.",
                    inline=False,
                )

        excluded_entries = self._get_excluded_entries()
        if not excluded_entries:
            embed.add_field(name="🚫 Excused", value="None", inline=False)
        else:
            excluded_lines = []
            for e in excluded_entries:
                e_class = await self._lookup_class_by_name(e["name"], cache=class_cache)
                e_icon = icons.resolve_class_icon(guild, e_class) if e_class else ""
                excluded_lines.append(
                    f"**#{e['id']}** — {e_icon} {e['name']} ({e['reason']})".replace("  ", " ")
                )
            excl_chunks, excl_truncated = _chunk_lines_for_embed_fields(excluded_lines, max_total_len=900)
            for i, chunk in enumerate(excl_chunks[:4]):
                field_name = "🚫 Excused" if i == 0 else "\u200b"
                embed.add_field(name=field_name, value=chunk, inline=False)
            if excl_truncated or len(excl_chunks) > 4:
                embed.add_field(name="\u200b", value="… excused list truncated.", inline=False)

        embed.set_footer(text=_footer_with_update_stamp(ROSTER_MARKER, updated_by))
        return embed

    async def _refresh_roster_message(self, guild: discord.Guild, updated_by: str = None):
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            return
        embed = await self._build_roster_embed(guild, updated_by=updated_by)

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
                "**The short version:** \n"
                "After each main-raid manually add the log to the log list (no efficient way to do this automatically, too many variables)\n"
                "This will then be used to calculate attendance. \n"
                f"to be Regular-eligible, someone needs to have "
                f"attended (killed at least `{config.ATTENDANCE_MIN_KILLS_PER_LOG}` boss) in "
                f"at least `{config.ATTENDANCE_MIN_ATTENDED}` of the last "
                f"`{config.ATTENDANCE_WINDOW}` tagged main raids.\n\n"
                "When people join a raid with their alts (and we want that to count towards attendance, you should add their character as an alt by using '/checkattendance link')"

                "**📋 Main Raid Log List** - the list of raids that actually count. "
                "Only logs added here are used - nothing is auto-detected from "
                "WarcraftLogs, so a random alt run or an off-night log never sneaks in. "
                "Use the ➕ Add Log / ➖ Remove Log buttons (Remove asks for the #ID shown "
                "next to each entry), or `/checkattendance addlog` / `removelog id:`.\n\n"

                "**🧾 Raider Roster** - shows every Regular member, plus Fresh members who've "
                f"actually shown up in at least one of the last `{config.ROSTER_FRESH_ACTIVITY_WINDOW}` "
                "logs (keeps this readable even with a large Fresh population). Main character "
                "and any alts shown with class icons. If someone's Discord nickname doesn't "
                "match their character name, fix it with `/checkattendance setmain`. Alts are "
                "added with `/checkattendance link`\n\n. The 🚫 Excused section at the bottom lists "
                "anyone sitting out attendance tracking (vacation, break, etc.) - **+ Add player** "
                "asks for a name and reason; **- Remove player** "
                "asks for the #ID next to their entry. `/checkattendance exclude name: reason:` "
                "offers real live name suggestions as you type, if you'd rather use the command; "
                "removal by command is `/checkattendance removeexcluded id:`.\n\n"

                "**📊 Attendance Overview** - the actual promotion/demotion discussion "
                "aid. Click Refresh (or run `/checkattendance run`, which has no cooldown(DON'T ABUSE)) "
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

    async def _tracked_message_still_exists(self, message_key: str) -> bool:
        """Checks whether a message we have a stored ID for actually still
        exists in Discord - not just whether our own local record has an
        ID. A cheap single fetch, so safe to do even before deciding
        whether to run the expensive roster/overview rebuild. Without this,
        a message deleted manually (outside the bot) would be believed to
        still exist forever, since the local record is never told."""
        record = self.bot.store.get(message_key)
        message_id = (record or {}).get("message_id")
        if not message_id:
            return False
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            return False    
        try:
            await channel.fetch_message(message_id)
            return True
        except (discord.NotFound, discord.Forbidden):
            return False
        except Exception:
            log.exception("Unexpected error checking whether message %s still exists", message_id)
            return False

    @commands.Cog.listener()
    async def on_ready(self):
        overall_start = time.monotonic()
        log.info("Attendance startup: beginning (this is usually the slow part - roster and "
                  "overview each make many sequential WCL API calls) ...")

        log.info("Attendance startup: checking explainer message...")
        await self._ensure_explainer_message()

        log.info("Attendance startup: checking log-list message...")
        await self._sync_log_list_message(None)

        guild_id = os.environ.get("DISCORD_GUILD_ID")
        guild = self.bot.get_guild(int(guild_id)) if guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is not None:
            if not await self._tracked_message_still_exists(ROSTER_MESSAGE_KEY):
                log.info(
                    "Attendance startup: no live roster message found (missing, or never "
                    "posted) - building one now (fetches recent-activity data + a class "
                    "lookup per raider/alt, can take a while the first time) ..."
                )
                step_start = time.monotonic()
                await self._refresh_roster_message(guild)
                log.info("Attendance startup: roster message done (%.1fs)", time.monotonic() - step_start)
            else:
                log.info("Attendance startup: roster message already exists, skipping rebuild")

            if not await self._tracked_message_still_exists(OVERVIEW_MESSAGE_KEY):
                log.info(
                    "Attendance startup: no live overview message found (missing, or never "
                    "posted) - computing one now (fetches every kill fight's roster across "
                    "the tracked log window) ..."
                )
                step_start = time.monotonic()
                await self._refresh_overview_message(guild)
                log.info("Attendance startup: overview message done (%.1fs)", time.monotonic() - step_start)
            else:
                log.info("Attendance startup: overview message already exists, skipping rebuild")

        log.info(
            "=== Attendance startup: COMPLETE (%.1fs total) - bot should be fully responsive now ===",
            time.monotonic() - overall_start,
        )

    # --- shared action logic (used by both slash commands and buttons) --

    async def _do_addlog(self, guild, date: str, tier: str, link: str, updated_by: str = None) -> str:
        report_code = _extract_report_code(link)
        record = self._get_log_list()
        next_id = (max((e["id"] for e in record["entries"]), default=0)) + 1
        record["entries"].append({
            "id": next_id, "date": date.strip(), "tier": tier.strip(), "report_code": report_code,
        })
        self._save_log_list(record)
        await self._sync_log_list_message(guild, updated_by=updated_by)
        return f"Added log #{next_id} ({date.strip()} · {tier.strip()})."

    async def _do_removelog(self, guild, log_id: int, updated_by: str = None) -> str:
        record = self._get_log_list()
        before = len(record["entries"])
        record["entries"] = [e for e in record["entries"] if e["id"] != log_id]
        if len(record["entries"]) == before:
            return f"No log with ID #{log_id} found."
        self._save_log_list(record)
        await self._sync_log_list_message(guild, updated_by=updated_by)
        return f"Removed log #{log_id}."

    async def _do_exclude(self, guild, name: str, reason: str) -> str:
        entries = self._get_excluded_entries()
        name = name.strip()
        reason = reason.strip()

        if any(e["name"].lower() == name.lower() for e in entries):
            return f"**{name}** is already excused."

        next_id = (max((e["id"] for e in entries), default=0)) + 1
        entries.append({"id": next_id, "name": name, "reason": reason})
        self._save_excluded_entries(entries)
        return (
            f"Excused **{name}** (#{next_id}) - reason: {reason}. "
            "Click 🔄 Refresh Roster to update the displayed list."
        )

    async def _do_remove_excluded(self, guild, excluded_id: int) -> str:
        entries = self._get_excluded_entries()
        match = next((e for e in entries if e["id"] == excluded_id), None)
        if match is None:
            return f"No excused entry with ID #{excluded_id} found."

        entries = [e for e in entries if e["id"] != excluded_id]
        self._save_excluded_entries(entries)

        overrides = self._get_baseline_overrides()
        overrides[match["name"].lower()] = config.ATTENDANCE_INCLUDE_BASELINE
        self._save_baseline_overrides(overrides)

        return (
            f"**{match['name']}** (#{excluded_id}) is back on attendance tracking - assumed "
            f"{config.ATTENDANCE_INCLUDE_BASELINE}/{config.ATTENDANCE_WINDOW} for the next "
            "`/checkattendance run` (or Refresh click) only, then real log data takes over again. "
            "Click 🔄 Refresh Roster to update the displayed list."
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
            await asyncio.sleep(0.2)  # pacing between reports, not just within one report's fights

        alt_links = self._get_alt_links()
        excluded = self._get_excluded_names()
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

    def _render_overview_embed(self, results: dict, updated_by: str = None) -> discord.Embed:
        embed = discord.Embed(
            title="📊 Attendance Overview",
            description=(
                "⚠️ **Role changes are manual** - this is a discussion aid, not an automatic action.\n\n"
                f"Based on the '{results['window_size']}'' most recent tagged main-raid log(s)\n"
                f"Players need {config.ATTENDANCE_MIN_ATTENDED}/{config.ATTENDANCE_WINDOW} to be Regular-eligible)."
            ),
            color=discord.Color.blurple(),
        )

        def _fmt(lines):
            if not lines:
                return "None"
            value = "\n".join(lines)
            if len(value) <= 1024:
                return value
            kept, total = [], 0
            for line in lines:
                if total + len(line) + 1 > 1000:
                    break
                kept.append(line)
                total += len(line) + 1
            return "\n".join(kept) + f"\n… +{len(lines) - len(kept)} more"

        def _rows(rows):
            return [f"**{name}** — {count}/{window}" for name, count, window in rows]

        watch_lines = (
            [f"⬆️ **{name}** — {count}/{window}" for name, count, window in results["watch_promote"]]
            + [f"⬇️ **{name}** — {count}/{window}" for name, count, window in results["watch_demote"]]
        )

        embed.add_field(name="✅ Promotion eligible (Fresh → Regular)", value=_fmt(_rows(results["promote"])), inline=False)
        embed.add_field(name="⚠️ Demotion review (Below Regular threshold)", value=_fmt(_rows(results["demote"])), inline=False)
        embed.add_field(
            name="👀 On watch for next raid (⬆️ could be promoted / ⬇️ could be demoted)",
            value=_fmt(watch_lines),
            inline=False,
        )
        embed.add_field(name="🚫 Excluded", value=", ".join(results["excluded"]) or "None", inline=False)
        if updated_by:
            when = datetime.now(timezone.utc).astimezone(AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M")
            embed.set_footer(text=f"Last updated by {updated_by} · {when}")
        return embed

    async def _refresh_overview_message(self, guild: discord.Guild, updated_by: str = None):
        """Returns the rendered overview Embed on success (or None on
        failure) - callers that don't need it just ignore the return value.
        Every refresh that has a real updated_by (i.e. not the silent
        startup reconciliation pass) also mirrors the result to
        MODERATOR_CHANNEL_ID if configured - see _notify_moderator_channel -
        regardless of whether it came from the button, /checkattendance run,
        or cogs/raid_logs.py's Summarize automation, since they all funnel
        through this one method."""
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            return None

        results = await self._compute_attendance(guild)
        embed = self._render_overview_embed(results, updated_by=updated_by)

        record = self.bot.store.get(OVERVIEW_MESSAGE_KEY)
        posted = False
        if record and record.get("message_id"):
            try:
                message = await channel.fetch_message(record["message_id"])
                await message.edit(embed=embed, view=self.overview_view)
                posted = True
            except (discord.NotFound, discord.Forbidden):
                pass

        if not posted:
            try:
                message = await channel.send(embed=embed, view=self.overview_view)
                self.bot.store.set(OVERVIEW_MESSAGE_KEY, message_id=message.id)
                posted = True
            except Exception:
                log.exception("Failed to post the attendance overview message")
                return None

        if updated_by:
            await self._notify_moderator_channel(embed, updated_by)
        return embed

    async def _notify_moderator_channel(self, overview_embed: discord.Embed, updated_by: str):
        if not self.moderator_channel_id:
            return
        channel = self.bot.get_channel(self.moderator_channel_id)
        if channel is None:
            log.warning("MODERATOR_CHANNEL_ID set but channel not found/visible")
            return
        try:
            await channel.send(f"📊 **Attendance overview refreshed** by {updated_by}.", embed=overview_embed)
        except Exception:
            log.exception("Failed to post the attendance overview notice to MODERATOR_CHANNEL_ID")

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
        await self._refresh_roster_message(interaction.guild, updated_by=interaction.user.display_name)
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
        await self._refresh_roster_message(interaction.guild, updated_by=interaction.user.display_name)
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
        await self._refresh_roster_message(interaction.guild, updated_by=interaction.user.display_name)
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
        await self._refresh_roster_message(interaction.guild, updated_by=interaction.user.display_name)
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

    async def _exclude_name_autocomplete(self, interaction: discord.Interaction, current: str):
        candidates = self._get_roster_candidate_names(interaction.guild)
        current_lower = current.lower()
        matches = [n for n in candidates if current_lower in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in matches[:25]]

    @checkattendance_group.command(name="exclude", description="Excuse a player from attendance tracking")
    @app_commands.describe(name="Their main character's name", reason="Why they're excused, e.g. vacation")
    @app_commands.autocomplete(name=_exclude_name_autocomplete)
    async def exclude(self, interaction: discord.Interaction, name: str, reason: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can exclude players.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmation = await self._do_exclude(interaction.guild, name, reason)
        await interaction.followup.send(confirmation, ephemeral=True)

    @checkattendance_group.command(name="removeexcluded", description="Remove a player from the excused list by ID")
    @app_commands.describe(id="The #ID shown next to the entry in the Excused Players message")
    async def removeexcluded(self, interaction: discord.Interaction, id: int):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage excused players.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmation = await self._do_remove_excluded(interaction.guild, id)
        await interaction.followup.send(confirmation, ephemeral=True)

    @checkattendance_group.command(name="run", description="Generate/refresh the attendance overview (no cooldown)")
    async def run(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can run attendance checks.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._refresh_overview_message(interaction.guild, updated_by=interaction.user.display_name)
        await interaction.followup.send("Overview posted/refreshed.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AttendanceCog(bot))