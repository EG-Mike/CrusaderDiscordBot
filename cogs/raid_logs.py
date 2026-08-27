"""
Raid log tagging - turns the "attach main-raid log / refresh roster /
refresh overview / notify mods / run /raidsummary" checklist that used to
be five manual steps in #attendance-check after every raid into: tag the
log once, then click (or wait for) Summarize.

Design, per discussion:
  - The bot does NOT own the #logs channel - a third-party webhook/app
    (e.g. "Crusader's Logs", see RAID_LOGS_CHANNEL_ID) posts there, so this
    bot can only read those messages, never attach its own buttons to
    them. Instead: RAID_LOGS_CHANNEL_ID's channel is meant to be hidden
    from members (View Channel denied for @everyone, kept for the bot's
    own role) and this cog reposts a cleaned-up version - reporter, when
    it started, the zone/description, a link, nothing else (explicitly NOT
    the Wipefest tool link or the /listen-bot instructions block some
    third-party posts include) - to RAID_LOGS_REPOST_CHANNEL_ID, with real
    bot-owned buttons attached. This was chosen over having the bot poll
    the WarcraftLogs API directly for new reports: WCL has no "report
    created" webhook, so that would mean a new polling loop (infrastructure
    this bot has never needed) re-solving report-detection filtering the
    existing log-posting bot already handles.
  - Every repost moves through one state machine: Untagged -> Tagged
    (Main Raid / Alt Raid / Other, Organizer-only) -> Summarized. Reset
    only exists pre-Summarize (Organizer-only) - once a log is
    Summarized, this channel is done being a control surface for it;
    corrections happen through #attendance-check's existing manual
    remove-log path, not by resetting here. The tag itself is always shown
    as plain embed text (not a button), so it stays a legible, permanent
    label on the message even after every action button is gone.
  - Summarize (moderator-only - broader than the Organizer-only tag gate)
    is triggered either by a manual click, or automatically once
    RAID_LOG_AUTO_SUMMARIZE_TIME (see config.py) passes on the day the log
    was tagged - see _auto_summarize_loop. This is deliberately NOT based
    on WarcraftLogs' own "is this report still live" signal
    (report.endTime == 0 while a log is in progress, confirmed via WCL's
    own API docs) or a boss-kill trigger: both look identical to "raid
    over" on a report that's simply paused between two nights of the same
    multi-night raid, which this guild runs regularly early in a tier. A
    dumb same-day wall-clock cutoff has no such failure mode, since
    main/alt raids reliably start 20:00-20:30 and never run past midnight.
  - What Summarize actually runs depends on the tag - "Main Raid" attaches
    the log to attendance.py's tracked list, refreshes the roster and
    overview messages (stamped with who/when - see attendance.py's
    updated_by param), and posts a notification to #attendance-check with
    a button to continue into /raidsummary. "Alt Raid" skips the
    attendance steps entirely and posts that same continue-to-/raidsummary
    button in this channel instead. "Other" gets no Summarize button and
    no automation at all - it's a label only; post a summary manually with
    /raidsummary if one's ever actually needed for one.
  - The Gargul loot paste always needs a human at the keyboard, so even an
    AUTOMATIC Summarize can only run the attendance side unattended - the
    "Post Raid Summary" button/prompt is what's left for a moderator to
    finish by hand either way (see _post_summary_prompt). That continue
    button reuses raid_summary.py's own RaidSummaryOptionsView/
    RaidSummaryCreateModal flow (with the report and main/alt pick already
    filled in via its preset_report_code/preset_raid_type params) rather
    than duplicating that flow here.
  - Duplicate live-logs: it's common for two people to each start a live
    log for the same raid at once, which the third-party bot then posts
    as two separate messages. Detected here as "same zone/description text,
    started within RAID_LOG_DUPLICATE_WINDOW_MINUTES of an already-active
    (not yet Summarized) repost" - see _find_duplicate_entry. A match is
    folded into the existing repost (noted as "also started by X", never
    silently discarded - the original #logs message is still there, just
    hidden from members) instead of getting its own separate repost.
  - Reposted logs' structured data (reporter/description/report code,
    already parsed once here) is reused by both /raidsummary's report
    picker and #attendance-check's Add Log picker - see
    get_recent_entries_for_picker - rather than each re-scraping #logs
    embeds a second time.
  - Self-contained like every other cog here - a future feature is a new
    cog file, not changes to this one.

CAVEAT: the #logs embed parsing below (_parse_source_embed) is written
against one real example post (a "Crusader's Logs"-style "X started a new
report" / zone line / Tools+Wipefest field / "/listen" block layout), not
against every possible shape that webhook/app might use - same caveat
raid_summary.py's own _extract_log_report_code carries. It degrades
gracefully (a field it can't parse just means a blank reporter/description,
never a crash), but is worth watching against the first few real posts.
"""

import os
import re
import functools
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import config
from cogs.raid_summary import RaidSummaryOptionsView

log = logging.getLogger("wow-apply-bot.raidlogs")

# Same guild-local timezone convention as raid_summary.py's AMSTERDAM_TZ /
# attendance.py's AMSTERDAM_TZ - raid-log timestamps and the daily
# auto-summarize cutoff are both far more meaningful in local time than UTC.
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

# Same pattern as raid_summary.py's LOG_REPORT_URL_RE - deliberately loose
# (matches a report link anywhere inside a string) since the #logs embed's
# exact field layout isn't fixed.
LOG_REPORT_URL_RE = re.compile(r"warcraftlogs\.com/reports/([A-Za-z0-9]{8,20})")
REPORTER_RE = re.compile(r"^(.+?)\s+started a new report\.?$", re.IGNORECASE)

# Text markers that flag a description/field candidate as Wipefest/`/listen`
# tool noise rather than the actual zone/description line - see the module
# docstring's "explicitly NOT the Wipefest tool link" note.
_NOISE_MARKERS = ("wipefest", "/listen", "listen bot")

RAID_LOG_MARKER = "raid-log-repost"
HISTORY_KEY = "raid_log_history"
HISTORY_MAX = 100
AUTO_SUMMARIZE_CHECK_INTERVAL_MINUTES = 10

TAG_MAIN_CUSTOM_ID = "raidlog_tag_main_btn"
TAG_ALT_CUSTOM_ID = "raidlog_tag_alt_btn"
TAG_OTHER_CUSTOM_ID = "raidlog_tag_other_btn"
RESET_CUSTOM_ID = "raidlog_reset_btn"
SUMMARIZE_CUSTOM_ID = "raidlog_summarize_btn"
POSTSUMMARY_CUSTOM_ID = "raidlog_postsummary_btn"

TAG_CUSTOM_IDS = {"main": TAG_MAIN_CUSTOM_ID, "alt": TAG_ALT_CUSTOM_ID, "other": TAG_OTHER_CUSTOM_ID}
TAG_LABELS = {"main": "🛡️ Main Raid", "alt": "🎲 Alt Raid", "other": "📦 Other"}


def _looks_like_noise(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _NOISE_MARKERS)


def _parse_source_embed(embed: discord.Embed) -> dict:
    """
    Best-effort parse of a #logs post's embed into {"reporter",
    "description", "report_code"} (any of which can come back None) - see
    the module docstring's caveat. Wipefest/`/listen`-tool content is
    deliberately never carried into the repost.
    """
    reporter = None
    for candidate in (embed.title, (embed.description or "").split("\n")[0] if embed.description else None):
        if not candidate:
            continue
        match = REPORTER_RE.match(candidate.strip())
        if match:
            reporter = match.group(1).strip()
            break

    description = None
    if embed.description:
        for line in embed.description.split("\n"):
            line = line.strip()
            if line and not _looks_like_noise(line) and not REPORTER_RE.match(line):
                description = line
                break
    if description is None:
        for field in embed.fields:
            if _looks_like_noise(field.name or "") or _looks_like_noise(field.value or ""):
                continue
            if field.value and len(field.value) <= 80:
                description = field.value.strip()
                break

    report_code = None
    candidates = [embed.description, embed.url, embed.title] + [f.value for f in embed.fields]
    for text in candidates:
        if not text:
            continue
        match = LOG_REPORT_URL_RE.search(text)
        if match:
            report_code = match.group(1)
            break

    return {"reporter": reporter, "description": description, "report_code": report_code}


class RaidLogsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.organizer_role_id = config.ORGANIZER_ROLE_ID

        source_channel_id = os.environ.get("RAID_LOGS_CHANNEL_ID")
        self.source_channel_id = int(source_channel_id) if source_channel_id else None
        repost_channel_id = os.environ.get("RAID_LOGS_REPOST_CHANNEL_ID")
        self.repost_channel_id = int(repost_channel_id) if repost_channel_id else None
        # Optional - a Main Raid Summarize also mirrors the just-refreshed
        # Attendance Overview here (see _post_moderator_overview_notice), on
        # top of the #attendance-check notification, since that channel is
        # easy to miss unless a mod happens to be looking at it. Leave unset
        # to skip this extra post - nothing else here depends on it.
        moderator_channel_id = os.environ.get("MODERATOR_CHANNEL_ID")
        self.moderator_channel_id = int(moderator_channel_id) if moderator_channel_id else None

    async def cog_load(self):
        # One dummy view registering every custom_id this cog ever sends,
        # so old buttons keep working across a bot restart - same pattern
        # as raid_summary.py's Edit/Add Loot buttons and attendance.py's
        # three persistent views. Real per-message views are built fresh by
        # _build_view every time a message is sent/edited; this dummy is
        # never itself displayed.
        dummy = discord.ui.View(timeout=None)
        for button in self._all_possible_buttons():
            dummy.add_item(button)
        self.bot.add_view(dummy)

        if not self.source_channel_id or not self.repost_channel_id:
            log.info(
                "RAID_LOGS_CHANNEL_ID and/or RAID_LOGS_REPOST_CHANNEL_ID not set - "
                "raid log tagging/auto-summarize is disabled (attendance.py and "
                "raid_summary.py both still work standalone without it)."
            )
        if not self._auto_summarize_loop.is_running():
            self._auto_summarize_loop.start()

    def cog_unload(self):
        self._auto_summarize_loop.cancel()

    # --- permission checks (self-contained, same as other cogs) ----------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    async def _is_organizer(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.organizer_role_id for role in member.roles)

    # --- history index (recent repost message IDs, most-recent-last) -----

    def _get_history_ids(self) -> list:
        record = self.bot.store.get(HISTORY_KEY)
        return (record or {}).get("ids", [])

    def _append_history(self, message_id: int):
        ids = self._get_history_ids()
        ids.append(message_id)
        self.bot.store.set(HISTORY_KEY, ids=ids[-HISTORY_MAX:])

    def get_recent_entries_for_picker(self, limit: int = 25) -> list:
        """
        Structured, already-deduplicated recent-log data for /raidsummary's
        and attendance.py's Add Log pickers to build their dropdown options
        from - see the module docstring. Most recent first; entries with no
        report_code (parsing failed) are never surfaced as a pickable
        option.
        """
        entries = []
        for message_id in reversed(self._get_history_ids()):
            entry = self.bot.store.get(message_id)
            if not entry or not entry.get("report_code"):
                continue
            try:
                started_at = datetime.fromisoformat(entry["started_at"])
            except (KeyError, ValueError):
                continue
            local = started_at.astimezone(AMSTERDAM_TZ)
            description = entry.get("description") or "Report"
            reporter = entry.get("reporter") or "unknown"
            label = f"{description} — {reporter} ({local.strftime('%b %d')})"
            entries.append({
                "label": label[:100],
                "report_code": entry["report_code"],
                "description": entry.get("description"),
                "date_guess": local.strftime("%Y-%m-%d"),
                "created_at": started_at,
            })
            if len(entries) >= limit:
                break
        return entries

    # --- duplicate detection ----------------------------------------------

    def _find_duplicate_entry(self, description: str, started_at: datetime):
        """
        Returns (message_id, entry) for an existing NOT-YET-SUMMARIZED
        repost with the same description text, started within
        config.RAID_LOG_DUPLICATE_WINDOW_MINUTES - i.e. someone else already
        starting a live log for the same raid, not a genuinely different
        one. None if nothing matches - see the module docstring for why a
        match gets folded in rather than reposted separately.
        """
        if not description:
            return None
        window = timedelta(minutes=config.RAID_LOG_DUPLICATE_WINDOW_MINUTES)
        for message_id in reversed(self._get_history_ids()):
            entry = self.bot.store.get(message_id)
            if not entry or entry.get("summarized"):
                continue
            if (entry.get("description") or "").strip().lower() != description.strip().lower():
                continue
            try:
                existing_started = datetime.fromisoformat(entry["started_at"])
            except (KeyError, ValueError):
                continue
            if abs(started_at - existing_started) <= window:
                return message_id, entry
        return None

    # --- rendering ----------------------------------------------------------

    def _tier_guess(self, description: str):
        """Best-effort match of a log's zone text against the two tiers
        this bot actually tracks (config.CURRENT_TIER/PREVIOUS_TIER) - used
        only to pre-select (never silently trust) RaidSummaryOptionsView's
        tier dropdown. Never guesses at a tier this bot has no boss/
        encounter-ID data for (e.g. older content like Karazhan) - that data
        has to come from a live WCL debug_zones.py run, not be invented
        here, so those simply aren't offered as options at all right now,
        same as plain /raidsummary already behaves."""
        if not description:
            return None
        lowered = description.lower()
        for tier in (config.CURRENT_TIER, config.PREVIOUS_TIER):
            name = tier["name"]
            if name.lower() in lowered:
                return name
            parts = [p for p in re.split(r"[/+&, ]+", name.lower()) if p]
            if parts and all(p in lowered for p in parts):
                return name
        return None

    def _render_embed(self, entry: dict) -> discord.Embed:
        started_local = datetime.fromisoformat(entry["started_at"]).astimezone(AMSTERDAM_TZ)
        embed = discord.Embed(
            title=f"🪵 {entry.get('reporter') or 'Someone'} started a raid log",
            description=(
                f"**{entry.get('description') or 'Unknown zone'}**\n"
                f"📜 [View on Warcraft Logs](https://fresh.warcraftlogs.com/reports/{entry['report_code']})\n"
                f"🕐 Started {started_local.strftime('%b %d, %H:%M')}"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Tag", value=TAG_LABELS.get(entry.get("tag"), "🟡 Untagged"), inline=True)

        if entry.get("summarized"):
            who = f"<@{entry['summarized_by']}>" if entry.get("summarized_by") else "auto-complete"
            when = datetime.fromisoformat(entry["summarized_at"]).astimezone(AMSTERDAM_TZ).strftime("%b %d, %H:%M")
            auto_note = f" (auto-completed at the {config.RAID_LOG_AUTO_SUMMARIZE_TIME} cutoff)" if entry.get("auto_summarized") else ""
            embed.add_field(name="Status", value=f"✅ Summarized by {who} at {when}{auto_note}", inline=True)
        elif entry.get("tag"):
            who = f"<@{entry['tagged_by']}>" if entry.get("tagged_by") else "?"
            embed.add_field(name="Tagged by", value=who, inline=True)

        extra = entry.get("extra_reports") or []
        if extra:
            lines = [f"• {e.get('reporter') or 'someone'} (report `{e.get('report_code')}`)" for e in extra]
            embed.add_field(name="⚠️ Also started around the same time (treated as duplicate)", value="\n".join(lines)[:1024], inline=False)

        embed.set_footer(text=RAID_LOG_MARKER)
        return embed

    def _tag_button(self, label: str, tag_value: str, style: discord.ButtonStyle) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, custom_id=TAG_CUSTOM_IDS[tag_value])
        button.callback = functools.partial(self._on_tag_click, tag_value=tag_value)
        return button

    def _all_possible_buttons(self) -> list:
        """Every custom_id/callback this cog ever sends, for the cog_load
        restart-survival registration - see cog_load."""
        buttons = [
            self._tag_button("Main Raid", "main", discord.ButtonStyle.success),
            self._tag_button("Alt Raid", "alt", discord.ButtonStyle.primary),
            self._tag_button("Other", "other", discord.ButtonStyle.secondary),
        ]
        reset_button = discord.ui.Button(label="Reset", style=discord.ButtonStyle.danger, custom_id=RESET_CUSTOM_ID)
        reset_button.callback = self._on_reset
        buttons.append(reset_button)
        summarize_button = discord.ui.Button(label="Summarize Raid", style=discord.ButtonStyle.success, custom_id=SUMMARIZE_CUSTOM_ID)
        summarize_button.callback = self._on_summarize
        buttons.append(summarize_button)
        postsummary_button = discord.ui.Button(label="📝 Post Raid Summary", style=discord.ButtonStyle.success, custom_id=POSTSUMMARY_CUSTOM_ID)
        postsummary_button.callback = self._on_postsummary_click
        buttons.append(postsummary_button)
        return buttons

    def _build_view(self, entry: dict) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        if entry.get("summarized"):
            return view  # no action buttons left - the tag stays visible as embed text only
        if not entry.get("tag"):
            view.add_item(self._tag_button("Main Raid", "main", discord.ButtonStyle.success))
            view.add_item(self._tag_button("Alt Raid", "alt", discord.ButtonStyle.primary))
            view.add_item(self._tag_button("Other", "other", discord.ButtonStyle.secondary))
            return view

        reset_button = discord.ui.Button(label="Reset", style=discord.ButtonStyle.danger, custom_id=RESET_CUSTOM_ID)
        reset_button.callback = self._on_reset
        view.add_item(reset_button)
        if entry["tag"] in ("main", "alt"):
            summarize_button = discord.ui.Button(label="Summarize Raid", style=discord.ButtonStyle.success, custom_id=SUMMARIZE_CUSTOM_ID)
            summarize_button.callback = self._on_summarize
            view.add_item(summarize_button)
        return view

    async def _render_and_edit(self, message: discord.Message, entry: dict):
        try:
            await message.edit(embed=self._render_embed(entry), view=self._build_view(entry))
        except (discord.NotFound, discord.Forbidden):
            pass

    # --- new-log detection ---------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.source_channel_id or not self.repost_channel_id:
            return
        if message.guild is None or message.channel.id != self.source_channel_id:
            return
        for embed in message.embeds:
            parsed = _parse_source_embed(embed)
            if not parsed["report_code"]:
                continue
            await self._handle_new_log(message, parsed)
            break  # one entry per message is enough - same convention as raid_summary.py

    async def _handle_new_log(self, source_message: discord.Message, parsed: dict):
        repost_channel = self.bot.get_channel(self.repost_channel_id)
        if repost_channel is None:
            log.warning("RAID_LOGS_REPOST_CHANNEL_ID set but channel not found/visible")
            return

        started_at = source_message.created_at  # aware UTC - the message posts right as the live log starts

        duplicate = self._find_duplicate_entry(parsed["description"], started_at)
        if duplicate is not None:
            dup_message_id, dup_entry = duplicate
            dup_entry.setdefault("extra_reports", []).append({
                "reporter": parsed["reporter"], "report_code": parsed["report_code"],
                "started_at": started_at.isoformat(),
            })
            self.bot.store.set(dup_message_id, **dup_entry)
            try:
                dup_message = await repost_channel.fetch_message(dup_message_id)
                await self._render_and_edit(dup_message, dup_entry)
            except (discord.NotFound, discord.Forbidden):
                pass
            log.info(
                "Report %s treated as a duplicate live-log of %s (same zone '%s', within %sm) - not reposted separately",
                parsed["report_code"], dup_entry.get("report_code"), parsed["description"],
                config.RAID_LOG_DUPLICATE_WINDOW_MINUTES,
            )
            return

        entry = {
            "source_message_id": source_message.id,
            "report_code": parsed["report_code"],
            "reporter": parsed["reporter"],
            "description": parsed["description"],
            "started_at": started_at.isoformat(),
            "tag": None, "tagged_by": None, "tagged_at": None,
            "summarized": False, "summarized_by": None, "summarized_at": None,
            "auto_summarized": False, "extra_reports": [],
        }
        try:
            repost_message = await repost_channel.send(embed=self._render_embed(entry), view=self._build_view(entry))
        except Exception:
            log.exception("Failed to repost new raid log to RAID_LOGS_REPOST_CHANNEL_ID")
            return

        self.bot.store.set(repost_message.id, **entry)
        self._append_history(repost_message.id)

    # --- tag / reset / summarize button callbacks -------------------------

    async def _on_tag_click(self, interaction: discord.Interaction, tag_value: str):
        entry = self.bot.store.get(interaction.message.id)
        if entry is None:
            await interaction.response.send_message("Couldn't find this log's data.", ephemeral=True)
            return
        if not await self._is_organizer(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only Organizers can tag raid logs.", ephemeral=True)
            return
        if entry.get("summarized"):
            await interaction.response.send_message("This log has already been summarized.", ephemeral=True)
            return

        entry["tag"] = tag_value
        entry["tagged_by"] = interaction.user.id
        entry["tagged_at"] = datetime.now(timezone.utc).isoformat()
        self.bot.store.set(interaction.message.id, **entry)
        await interaction.response.edit_message(embed=self._render_embed(entry), view=self._build_view(entry))

    async def _on_reset(self, interaction: discord.Interaction):
        entry = self.bot.store.get(interaction.message.id)
        if entry is None:
            await interaction.response.send_message("Couldn't find this log's data.", ephemeral=True)
            return
        if not await self._is_organizer(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only Organizers can reset a log's tag.", ephemeral=True)
            return
        if entry.get("summarized"):
            await interaction.response.send_message(
                "This log has already been summarized - Reset is only available before that.", ephemeral=True
            )
            return

        entry["tag"] = None
        entry["tagged_by"] = None
        entry["tagged_at"] = None
        self.bot.store.set(interaction.message.id, **entry)
        await interaction.response.edit_message(embed=self._render_embed(entry), view=self._build_view(entry))

    async def _on_summarize(self, interaction: discord.Interaction):
        entry = self.bot.store.get(interaction.message.id)
        if entry is None:
            await interaction.response.send_message("Couldn't find this log's data.", ephemeral=True)
            return
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can summarize a raid log.", ephemeral=True)
            return
        if entry.get("summarized"):
            await interaction.response.send_message("Already summarized.", ephemeral=True)
            return
        if entry.get("tag") not in ("main", "alt"):
            await interaction.response.send_message("Tag this log Main Raid or Alt Raid first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._run_summarize(interaction.guild, interaction.message, entry, triggered_by=interaction.user, is_auto=False)
        await interaction.followup.send("Done.", ephemeral=True)

    # --- Summarize automation ---------------------------------------------

    async def _run_summarize(self, guild: discord.Guild, message: discord.Message, entry: dict,
                              triggered_by, is_auto: bool):
        entry["summarized"] = True
        entry["summarized_by"] = triggered_by.id if triggered_by else None
        entry["summarized_at"] = datetime.now(timezone.utc).isoformat()
        entry["auto_summarized"] = is_auto
        self.bot.store.set(message.id, **entry)
        await self._render_and_edit(message, entry)

        who_label = triggered_by.display_name if triggered_by else f"auto-complete ({config.RAID_LOG_AUTO_SUMMARIZE_TIME} cutoff)"
        tier_guess = self._tier_guess(entry.get("description"))

        if entry["tag"] == "main":
            await self._run_main_automation(guild, entry, who_label, tier_guess, is_auto)
        elif entry["tag"] == "alt":
            await self._post_summary_prompt(
                self.repost_channel_id, entry, raid_type="alt",
                tier_guess=tier_guess, who_label=who_label, is_auto=is_auto,
            )

    async def _run_main_automation(self, guild: discord.Guild, entry: dict, who_label: str, tier_guess, is_auto: bool):
        attendance_cog = self.bot.get_cog("AttendanceCog")
        if attendance_cog is None:
            log.warning("AttendanceCog isn't loaded - skipping main-raid attendance automation")
            return

        started_local = datetime.fromisoformat(entry["started_at"]).astimezone(AMSTERDAM_TZ)
        date_str = started_local.strftime("%Y-%m-%d")
        tier_label = entry.get("description") or tier_guess or "Main Raid"

        await attendance_cog._do_addlog(guild, date_str, tier_label, entry["report_code"], updated_by=who_label)
        await attendance_cog._refresh_roster_message(guild, updated_by=who_label)
        overview_embed = await attendance_cog._refresh_overview_message(guild, updated_by=who_label)
        await self._post_summary_prompt(
            attendance_cog.attendance_channel_id, entry, raid_type="main",
            tier_guess=tier_guess, who_label=who_label, is_auto=is_auto,
        )
        await self._post_moderator_overview_notice(overview_embed, who_label)

    async def _post_moderator_overview_notice(self, overview_embed, who_label: str):
        if not self.moderator_channel_id or overview_embed is None:
            return
        channel = self.bot.get_channel(self.moderator_channel_id)
        if channel is None:
            log.warning("MODERATOR_CHANNEL_ID set but channel not found/visible")
            return
        try:
            await channel.send(
                f"📊 **Attendance overview is updated** after the last raid (refreshed by {who_label}).",
                embed=overview_embed,
            )
        except Exception:
            log.exception("Failed to post the attendance overview notice to MODERATOR_CHANNEL_ID")

    async def _post_summary_prompt(self, channel_id, entry: dict, raid_type: str, tier_guess, who_label: str, is_auto: bool):
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            log.warning("Couldn't find channel %s to post the raid-summary prompt", channel_id)
            return

        auto_note = (
            f" *(auto-completed at the {config.RAID_LOG_AUTO_SUMMARIZE_TIME} cutoff - "
            "the loot paste step below still needs a moderator)*" if is_auto else ""
        )
        if raid_type == "main":
            text = (
                f"🔔 **Attendance updated** - log `{entry['report_code']}` attached, roster & overview "
                f"refreshed by {who_label}.{auto_note}"
            )
        else:
            text = f"🎲 **Alt raid log tagged** by {who_label}.{auto_note}"

        view = discord.ui.View(timeout=None)
        button = discord.ui.Button(label="📝 Post Raid Summary", style=discord.ButtonStyle.success, custom_id=POSTSUMMARY_CUSTOM_ID)
        button.callback = self._on_postsummary_click
        view.add_item(button)

        try:
            message = await channel.send(text, view=view)
        except Exception:
            log.exception("Failed to post the raid-summary prompt")
            return

        self.bot.store.set(
            message.id, kind="raidlog_summary_prompt", report_code=entry["report_code"],
            description=entry.get("description"), raid_type=raid_type, tier_guess=tier_guess,
        )

    async def _on_postsummary_click(self, interaction: discord.Interaction):
        record = self.bot.store.get(interaction.message.id)
        if not record or record.get("kind") != "raidlog_summary_prompt":
            await interaction.response.send_message("Couldn't find this prompt's data.", ephemeral=True)
            return
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can post raid summaries.", ephemeral=True)
            return

        raid_summary_cog = self.bot.get_cog("RaidSummaryCog")
        if raid_summary_cog is None:
            await interaction.response.send_message("The raid summary feature isn't loaded.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Pick the tier and clear status, then hit **Continue** for loot/note/media.",
            view=RaidSummaryOptionsView(
                raid_summary_cog, [], preset_report_code=record["report_code"],
                preset_raid_type=record["raid_type"], default_tier=record.get("tier_guess"),
            ),
            ephemeral=True,
        )

    # --- daily auto-summarize cutoff ---------------------------------------

    def _resolve_guild(self):
        guild_id = os.environ.get("DISCORD_GUILD_ID")
        if guild_id:
            return self.bot.get_guild(int(guild_id))
        return self.bot.guilds[0] if self.bot.guilds else None

    @tasks.loop(minutes=AUTO_SUMMARIZE_CHECK_INTERVAL_MINUTES)
    async def _auto_summarize_loop(self):
        if not self.repost_channel_id:
            return
        guild = self._resolve_guild()
        if guild is None:
            return

        now = datetime.now(AMSTERDAM_TZ)
        cutoff_hour, cutoff_minute = (int(p) for p in config.RAID_LOG_AUTO_SUMMARIZE_TIME.split(":"))
        cutoff = now.replace(hour=cutoff_hour, minute=cutoff_minute, second=0, microsecond=0)
        if now < cutoff:
            return

        channel = self.bot.get_channel(self.repost_channel_id)
        if channel is None:
            return

        for message_id in self._get_history_ids():
            entry = self.bot.store.get(message_id)
            if not entry or entry.get("summarized") or entry.get("tag") not in ("main", "alt"):
                continue
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                continue
            log.info(
                "Auto-summarizing raid log %s (tagged '%s', past today's %s cutoff, never manually Summarized)",
                entry["report_code"], entry["tag"], config.RAID_LOG_AUTO_SUMMARIZE_TIME,
            )
            await self._run_summarize(guild, message, entry, triggered_by=None, is_auto=True)

    @_auto_summarize_loop.before_loop
    async def _before_auto_summarize_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidLogsCog(bot))
