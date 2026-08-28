"""
Raid summary feature - posts a presentable per-raid recap (banner, links to
the full log + Wipefest analysis, a compact loot list with real item icons/
Wowhead links, boss-by-boss pulls with kill time + wipe breakdown + fastest-
kill tracking, elite parses, personal bests, top damage, roster composition,
guild rank, a death leaderboard) as a new thread in a Discord forum channel,
so raiders have somewhere to discuss each raid night.

Design, per discussion:
  - /raidsummary takes NO slash-command options - it immediately shows
    RaidSummaryOptionsView, an ephemeral message with native dropdowns for
    tier, full-clear-or-progress, main-or-alt raid, and (when
    RAID_LOGS_CHANNEL_ID is configured) a report picker populated from
    recent posts in the #logs channel, labeled by their report title
    instead of a raw link (see _fetch_recent_log_entries /
    _extract_log_report_code). Hitting Continue there opens a modal
    (RaidSummaryCreateModal) for the fields that can't be a dropdown: a
    report-link fallback (pre-filled if one was picked, always editable -
    the #logs list is best-effort/recent-only, so an older report still
    needs a pasted link), the Gargul loot export, a note, and a media
    link. Two steps, not one, because Discord modals have no select-menu
    support at all - only text fields - so anything meant to be a picker
    has to happen before the modal, not in it. The loot paste specifically
    HAS to go through the modal, not a slash-command string option - a
    plain option renders as a single-line input in Discord's client, so a
    multi-line paste into one gets every newline silently collapsed to a
    space (confirmed live: the parser then sees the whole export as one
    unparseable line). A modal's paragraph-style TextInput is the only
    Discord input that preserves real newlines. Loot can also be
    added/replaced later - see below - and a paste that lands exactly at
    the modal field's 4000-char ceiling is rejected rather than trusted,
    since Discord's input box silently truncates instead of refusing to
    submit - the rejection points the moderator at the 🎁 Add/Update Loot
    button's file upload instead, which has no such limit (a Gargul export
    runs ~50 chars/item, so 4000 chars still covers ~75-80 items -
    comfortably past a normal night).
  - All WCL data for the report (fights/pulls, parse rankings, deaths,
    damage done, healing done) comes from ONE cached fetch -
    wcl_client.WarcraftLogsClient.get_report_summary() - shared with
    cogs/attendance.py's attendance check. Roster composition (and the
    name -> class map used for icons everywhere below) is a SEPARATE,
    lazily-fetched call (get_report_role_composition) - see
    _build_comp_block()'s docstring for why that one isn't folded into the
    always-cheap summary fetch. Every name mentioned anywhere in the
    summary (parses, MVP's, damage/death leaderboards, loot winners) is
    prefixed with that character's class icon, same icon source
    /checkattendance's roster uses (see _name_icon()).
  - "First pull" is the first REAL boss pull (the earliest fight with a
    non-null encounter_id), not the report's own recorded start time -
    those can differ by several minutes of trash/travel before the actual
    first pull, and WCL's per-fight start/end times are relative offsets
    from the report's start, not absolute, so the first pull's real clock
    time is report start + that fight's relative offset. Total duration is
    measured from that same first-pull anchor to the raid's end, so all
    three numbers on that line stay mutually consistent. A wipe fight
    logged at 100% boss HP is a deliberate reset, not a real attempt, and
    is filtered out everywhere (pull counts, wipe listings, clear-time
    spans) - see _group_fights_by_encounter().
  - Forum tags: the tier and main/alt-raid tags are applied by exact
    Discord tag ID (config.TIER_TAG_IDS / RAID_TYPE_TAG_IDS); the clear-
    status tag is matched by name instead (config.CLEAR_STATUS_TAG_NAMES) -
    see _resolve_applied_tags().
  - Gargul's export gives itemID + winner only (no item name/icon/boss) -
    see gargul_loot.py. Item name/icon/Wowhead link are resolved via
    wowhead.py (permanently cached locally, itemID -> name never changes).
    Boss attribution is NOT attempted (no reliable source for it - see
    gargul_loot.py's docstring), so loot is shown as one flat list in
    award order, not grouped per boss.
  - Loot rendering: one compact line per item ("<icon> [Name](wowhead) →
    **Winner**"), not one Discord component per item - the icon is a real
    bot-owned emoji for that item (provisioned on demand via
    icons.ensure_item_emoji(), the same "download the icon, upload it as an
    application emoji" mechanism /add-emoji in cogs/emoji_admin.py already
    uses), falling back to a colored quality-square emoji if that couldn't
    be created. Loot always starts on its own fresh page (see
    _render_pages()) rather than sharing space with whatever's left on the
    last header page - normally the summary's 2nd message.
  - Banner: config.RAID_TIER_BANNERS maps a tier name to either a plain
    http(s) URL, or a local file path - Components V2 has no "image URL"
    field, so a local file gets attached to the summary's first message and
    referenced via Discord's attachment:// scheme (see _load_banner()). A
    missing/unconfigured banner just means no banner, never blocks posting.
  - Kill-time/clear-time/parse records: self._get_records() persists three
    things across raids - see RECORDS_KEY: (1) per encounter, the fastest
    kill time we've ever recorded; (2) per raid instance (see
    config.TIER_SUB_INSTANCES - some tiers, e.g. "BT/Hyjal", bundle two
    real WoW raid instances together, and clear time is tracked per real
    instance, not per tier), the fastest full clear; (3) per (encounter,
    character), that character's own best parse on that boss. Every posted
    summary compares against whatever's on record: kill/clear times get the
    delta + a ⚡ badge on a new (or tied) fastest, and any parse that beats
    a character's own prior best gets called out in "Personal bests broken
    tonight". Clear-time tracking is purely DATA-driven (every boss in that
    instance killed this raid), independent of the mod's Full Clear/
    Progress pick - a "Progress" raid can still show a clean per-instance
    clear if that wing got finished. A boss/instance/character with nothing
    on record yet just gets this raid's number recorded silently as the new
    baseline, nothing to compare against. Records are only ever updated by
    a fresh /raidsummary post, never by editing one or adding loot later.
  - Editing/adding loot later: the note, media link, and loot are each
    independently updatable after posting via persistent buttons on the
    summary's last message (✏️ Edit for note/media, 🎁 Add/Update Loot for
    loot - loot can't go through a modal like the other two since Discord
    modals have no file-upload field; that button instead asks the
    moderator to reply in the thread with the export and catches it via
    bot.wait_for()). Everything else (boss lines, parses, personal bests,
    damage, deaths, guild rank, comp) is computed ONCE at post time and
    frozen. This is deliberate, not a shortcut: those sections read from
    the fastest-kill/first-kill/personal-best records, which get UPDATED at
    post time - recomputing them later would make an already-posted "first
    kill!"/"fastest!"/personal-best line silently change (or disappear),
    since by then the record equals itself. Freezing them avoids that whole
    class of bug, and it's cheap since it's exactly what needed to stay
    editable per the ask.
  - Discord's Components V2 caps a single message at ~4000 chars of text
    across all TextDisplay components (and a component-count budget) - loot
    especially can blow past that for a big clear, so the whole summary is
    built as a list of small "blocks" which get packed into as many
    separate forum-thread messages as needed - see _paginate_blocks(). The
    first message becomes the thread's OP (with the tier + clear-status
    forum tags applied, and the banner attached if any); the rest are
    follow-up posts in the same thread. Editing/adding-loot re-paginates
    from the frozen blocks + fresh tldr/loot/footer content, and reconciles
    against however many messages existed before (see _reconcile_pages -
    edits messages in place, sends new ones if longer, deletes leftovers if
    shorter).
  - Fun stats: top-3 leaderboards for Activity %, combined Destruction+
    Haste potion use (headed by both potions' real Wowhead item icons),
    interrupts, dispels (both headed by a decorative Wowhead spell icon -
    Kick/Dispel Magic), overall damage, overall healing, and least-
    overhealed % (healers only - see _build_overheal_block). All share one
    renderer (_build_ranked_block) that also checks the #1 entry against
    this TIER's all-time best for that stat (records["tier_stats"][tier
    name][stat_key], via _check_stat_record) and tags it "🏆 New raid
    record!" when beaten - scoped per tier since e.g. a comp-dependent
    stat like damage isn't a fair cross-tier comparison, same reasoning
    kill-time/clear-time records are already scoped to one boss/instance
    rather than kept globally.

    Below those, a "Buff/Debuff Uptime" section for config.TRACKED_DEBUFFS
    (Sunder/Expose Armor, Faerie Fire, Curse of the Elements/Recklessness -
    kept on the boss) and config.TRACKED_BUFFS (Judgement of Wisdom/Light -
    kept on a player), each shown with both an "all fights" (bosses+trash)
    and a "boss fights only" uptime %, the raider who contributed the most
    of the boss-fight uptime, and a delta/⚡-new-best badge against
    records["buffs"][tier name] (boss-only percentage only - trash uptime
    is too inconsistent raid-to-raid to be a meaningful personal best) -
    see _build_uptime_lines and wcl_client.get_report_aura_uptime. All
    tracked ability lists are matched against WCL by NAME, not spell ID,
    since a rank's exact ID varies by who cast it while the name doesn't -
    see config.py's comment above TRACKED_DEBUFFS. Each tracked debuff/buff
    also gets a real Wowhead spell icon (config.TRACKED_ABILITY_ICON_SPELL_IDS
    - any correct rank's ID works there since the icon doesn't change
    across ranks), fetched once and provisioned as a bot-owned emoji the
    same way loot/potion icons are (icons.ensure_spell_emoji).
  - Self-contained like every other cog here - a future feature is a new
    cog file, not changes to this one.
"""

import os
import re
import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import config
import icons
import gargul_loot
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.raidsummary")

REPORT_LINK_RE = re.compile(r"(?:reports/|^)([A-Za-z0-9]{8,20})(?:[/#].*)?$")

# Used to pull a report code out of #logs channel embeds (see
# _fetch_recent_log_entries) - deliberately looser than REPORT_LINK_RE
# (which expects the WHOLE string to be a link/code) since here the code
# is embedded somewhere inside a title/description/field of unknown shape.
LOG_REPORT_URL_RE = re.compile(r"warcraftlogs\.com/reports/([A-Za-z0-9]{8,20})")
LOGS_HISTORY_LIMIT = 50
LOGS_SELECT_LIMIT = 25  # Discord's own per-select-menu option cap

RECORDS_KEY = "raid_summary_records"
# Prefix for the per-tier "every report code ever posted under this tier"
# roster - see _record_tier_report. Full store key is this + the tier's
# own name (e.g. "raid_summary_tier_reports:SSC/TK").
TIER_REPORTS_KEY_PREFIX = "raid_summary_tier_reports:"
EDIT_BUTTON_CUSTOM_ID = "raidsummary_edit_btn"
ADD_LOOT_BUTTON_CUSTOM_ID = "raidsummary_addloot_btn"
ADD_LOOT_WAIT_SECONDS = 300

# Discord's own hard ceiling for a modal TextInput field - not something we
# can raise (see RaidSummaryCreateModal's docstring for why the loot paste
# has to go through a modal, not a slash-command option, in the first
# place). A Gargul export is compact (roughly 50 chars/item, confirmed
# against a real 43-item export), so this covers ~75-80 items - comfortably
# past a normal raid night. Anything that hits this ceiling exactly is
# treated as possibly truncated by Discord's own input box rather than
# trusted - see _create_summary() - and pointed at the 🎁 Add/Update Loot
# button's file upload instead, which has no such limit.
GARGUL_TEXT_MAX_LENGTH = 4000

# Sanity cap on /raidsummary-bulk's report list - generous headroom over the
# ~15-16 reports it's actually meant for (one tier's worth of raid nights),
# just a guard against pasting the wrong thing rather than a tuned limit.
MAX_BULK_REPORTS = 50

QUALITY_EMOJI = {0: "⬜", 1: "⬜", 2: "🟩", 3: "🟦", 4: "🟪", 5: "🟧"}
DEFAULT_QUALITY_EMOJI = "⬜"

# Rank -> medal emoji for the damage/death leaderboards (1st-5th place).
RANK_MEDALS = ["🥇", "🥈", "🥉", "🏅", "🏅"]

# Same guild-local timezone convention already used elsewhere (see
# cogs/apply.py's AMSTERDAM_TZ) - raid start/end clock times are far more
# readable in local time than UTC.
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

# Conservative budget per forum-thread message - see the module docstring's
# note on Components V2's ~4000-char/40-component caps.
MAX_CHARS_PER_PAGE = 3500
MAX_UNITS_PER_PAGE = 24


def _extract_report_code(link: str) -> str:
    """Accepts a bare report code or a full WCL report URL."""
    link = link.strip().rstrip("/")
    match = REPORT_LINK_RE.search(link)
    return match.group(1) if match else link


def _extract_log_report_code(embed: discord.Embed) -> str | None:
    """Pulls a WCL report code out of a #logs channel post's embed. The
    exact field layout is up to whatever third-party webhook/app posts
    there (e.g. "Crusader's Logs") - rather than assume one fixed spot,
    this checks the description, every field value, the embed's own url,
    and the title, in that order, and returns the first WCL report link
    found anywhere in them."""
    candidates = [embed.description, embed.url, embed.title]
    candidates += [f.value for f in embed.fields]
    for text in candidates:
        if not text:
            continue
        match = LOG_REPORT_URL_RE.search(text)
        if match:
            return match.group(1)
    return None


def _wipefest_fight_url(report_code: str, fight_id: int) -> str:
    """Wipefest's per-fight deep link - confirmed live: WCL's fight ID
    (already recorded per fight in the report cache) is literally the same
    ID Wipefest uses in its own /fight/<id> URL segment, not a guess."""
    return f"https://www.wipefest.gg/report/{report_code}/fight/{fight_id}?gameVersion=warcraft-fresh"


def _boss_name(p: dict, fight_names: dict) -> str:
    """A parse's boss name, preferring whatever WCL's rankings JSON embeds
    directly on the entry, falling back to joining fight_id against the
    report's own fight list. See wcl_client._parse_rankings' docstring for
    why both exist."""
    return p.get("boss_name") or fight_names.get(p.get("fight_id"), "Unknown boss")


def _format_clock(ms) -> str:
    """'m:ss' or 'h:mm:ss' - for a single boss kill time, e.g. '4:39'."""
    if not ms or ms < 0:
        return "?"
    total_seconds = int(ms / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format_duration_compact(ms) -> str:
    """'1h47min' or '47min' - for the "First pull / Raid ended / Total
    duration" line."""
    if not ms or ms < 0:
        return "?"
    total_seconds = int(ms / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, _s = divmod(rem, 60)
    return f"{h}h{m}min" if h else f"{m}min"


def _format_duration_words(ms) -> str:
    """'57 minutes 12 seconds' or '1 hour 44 minutes 30 seconds' - matches
    WarcraftLogs' own clear-time phrasing, full precision (see
    _format_delta for the signed comparison used alongside it)."""
    if not ms or ms < 0:
        return "?"
    total_seconds = int(ms / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    parts.append(f"{m} minute{'s' if m != 1 else ''}")
    parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts)


def _format_delta(delta_ms) -> str:
    """Signed human delta, e.g. '-18 seconds' or '+2 minutes, 40 seconds'."""
    sign = "-" if delta_ms < 0 else "+"
    total_seconds = int(abs(delta_ms) / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s or (not h and not m):
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return f"{sign}{', '.join(parts)}"


class RaidSummaryEditModal(discord.ui.Modal, title="Edit Raid Summary"):
    note = discord.ui.TextInput(
        label="Note/highlight (optional)", style=discord.TextStyle.paragraph,
        required=False, max_length=300,
    )
    media_link = discord.ui.TextInput(
        # Discord caps TextInput labels at 45 chars - the original label
        # here was 48 and made every /raidsummary edit attempt 400 with
        # "Invalid Form Body" until this was noticed live.
        label="Media link (YouTube/Twitch/image)", required=False, max_length=300,
    )

    def __init__(self, cog: "RaidSummaryCog", last_message_id: int, prefill_note=None, prefill_media=None):
        super().__init__()
        self.cog = cog
        self.last_message_id = last_message_id
        if prefill_note:
            self.note.default = prefill_note
        if prefill_media:
            self.media_link.default = prefill_media

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog._apply_edit(
            interaction, self.last_message_id,
            str(self.note).strip() or None, str(self.media_link).strip() or None,
        )


class RaidSummaryOptionsView(discord.ui.View):
    """
    /raidsummary's first step, replacing what used to be four slash-
    command options (tier, report, clear_status, raid_type) - now the
    command takes no arguments at all and immediately shows this: three
    native dropdowns (tier, clear status, raid type) plus, when
    RAID_LOGS_CHANNEL_ID is configured, a fourth dropdown of recent
    reports pulled from the #logs channel (see
    RaidSummaryCog._fetch_recent_log_entries), labeled by their report
    title rather than a raw link/code. A modal has no select-menu support
    (only text fields), so this two-step "pick from dropdowns, then hit
    Continue for the modal" flow is the only way to keep every field a
    picker except the genuinely free-text ones (loot paste, note, media
    link) - those stay in RaidSummaryCreateModal, which Continue opens.
    Report is always still editable/typeable there too (pre-filled from
    this dropdown if one was picked) - the #logs list is best-effort and
    only ever shows the most recent LOGS_SELECT_LIMIT reports, so an
    older report still needs a pasted link.
    """
    def __init__(self, cog: "RaidSummaryCog", log_entries: list, preset_report_code: str = None,
                 preset_raid_type: str = None, default_tier: str = None):
        """
        preset_report_code/preset_raid_type let cogs/raid_logs.py's tagging
        workflow skip straight to this view with the report and main/alt
        pick already decided (both are already known by the time a tagged
        log gets Summarized - see RaidLogsCog._post_summary_prompt), rather
        than duplicating this whole dropdown-then-modal flow in a second
        place. When set, the corresponding select is never created at all -
        tier and clear-status still always need a human pick, since neither
        is reliably inferable (this bot only tracks CURRENT_TIER/
        PREVIOUS_TIER's exact two names - see _resolve_tier - and an older
        tier or alt-run zone can't be assumed to be either one).
        default_tier best-effort pre-selects the tier dropdown (still
        changeable) when the log's own zone text looks like a match -
        never trusted blindly, since guessing wrong and letting it through
        unnoticed would misfile boss-kill/clear-time records.
        """
        super().__init__(timeout=600)
        self.cog = cog
        self.tier = default_tier
        self.clear_status = None
        self.raid_type = preset_raid_type
        self.report_code = preset_report_code

        self.tier_select = discord.ui.Select(
            placeholder="Tier",
            options=[
                discord.SelectOption(label=config.CURRENT_TIER["name"], value=config.CURRENT_TIER["name"]),
                discord.SelectOption(label=config.PREVIOUS_TIER["name"], value=config.PREVIOUS_TIER["name"]),
            ],
        )
        self.tier_select.callback = self._on_tier_select
        self.add_item(self.tier_select)

        self.clear_status_select = discord.ui.Select(
            placeholder="Full Clear or Progress?",
            options=[
                discord.SelectOption(label="Full Clear", value="full_clear"),
                discord.SelectOption(label="Progress", value="progress"),
            ],
        )
        self.clear_status_select.callback = self._on_clear_status_select
        self.add_item(self.clear_status_select)

        self.raid_type_select = None
        if preset_raid_type is None:
            self.raid_type_select = discord.ui.Select(
                placeholder="Main Raid or Alt Raid?",
                options=[
                    discord.SelectOption(label="Main Raid", value="main"),
                    discord.SelectOption(label="Alt Raid", value="alt"),
                ],
            )
            self.raid_type_select.callback = self._on_raid_type_select
            self.add_item(self.raid_type_select)

        self.report_select = None
        if preset_report_code is None and log_entries:
            options = [
                discord.SelectOption(
                    label=entry["label"],
                    value=entry["report_code"],
                    description=entry["created_at"].astimezone(AMSTERDAM_TZ).strftime("%b %d, %H:%M"),
                )
                for entry in log_entries
            ]
            self.report_select = discord.ui.Select(
                placeholder="Report from #logs (optional - or paste a link in the next step)",
                options=options, min_values=0, max_values=1,
            )
            self.report_select.callback = self._on_report_select
            self.add_item(self.report_select)

        self.continue_button = discord.ui.Button(
            label="Continue", style=discord.ButtonStyle.primary, disabled=not self._ready()
        )
        self.continue_button.callback = self._on_continue
        self.add_item(self.continue_button)
        self._sync_selected_defaults()

    def _ready(self) -> bool:
        return self.tier is not None and self.clear_status is not None and self.raid_type is not None

    def _sync_selected_defaults(self):
        """Keeps each dropdown showing its picked value (instead of
        reverting to its placeholder) after a refresh - edit_message
        resends every component fresh, and Discord only displays a
        selection in place of the placeholder for whichever SelectOption
        currently has default=True, which isn't tracked automatically."""
        for opt in self.tier_select.options:
            opt.default = opt.value == self.tier
        for opt in self.clear_status_select.options:
            opt.default = opt.value == self.clear_status
        if self.raid_type_select:
            for opt in self.raid_type_select.options:
                opt.default = opt.value == self.raid_type
        if self.report_select:
            for opt in self.report_select.options:
                opt.default = opt.value == self.report_code

    async def _refresh(self, interaction: discord.Interaction):
        self.continue_button.disabled = not self._ready()
        self._sync_selected_defaults()
        await interaction.response.edit_message(view=self)

    async def _on_tier_select(self, interaction: discord.Interaction):
        self.tier = self.tier_select.values[0]
        await self._refresh(interaction)

    async def _on_clear_status_select(self, interaction: discord.Interaction):
        self.clear_status = self.clear_status_select.values[0]
        await self._refresh(interaction)

    async def _on_raid_type_select(self, interaction: discord.Interaction):
        self.raid_type = self.raid_type_select.values[0]
        await self._refresh(interaction)

    async def _on_report_select(self, interaction: discord.Interaction):
        self.report_code = self.report_select.values[0] if self.report_select.values else None
        await self._refresh(interaction)

    async def _on_continue(self, interaction: discord.Interaction):
        if not self._ready():
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            RaidSummaryCreateModal(self.cog, self.tier, self.report_code, self.clear_status, self.raid_type)
        )
        self.stop()


class RaidSummaryCreateModal(discord.ui.Modal, title="Post Raid Summary"):
    """
    Opened by RaidSummaryOptionsView's Continue button for the fields that
    can't be a dropdown: the report link (pre-filled if one was picked
    from #logs, but always editable/typeable for an older report or when
    RAID_LOGS_CHANNEL_ID isn't configured), the Gargul loot export, a
    note, and a media link. The loot paste specifically HAS to go through
    a modal, not a slash-command string option - a plain option renders
    as a single-line input in Discord's client, so a multi-line paste
    into one gets every newline silently collapsed to a space (confirmed
    live: the parser then sees the whole export as one unparseable line).
    A modal's paragraph-style TextInput is the only Discord input that
    preserves real newlines.
    """
    report_link = discord.ui.TextInput(
        label="Report link (if not picked above)", required=False, max_length=200,
    )
    gargul_export_text = discord.ui.TextInput(
        label="Gargul loot export (optional)", style=discord.TextStyle.paragraph,
        required=False, max_length=GARGUL_TEXT_MAX_LENGTH,
    )
    note = discord.ui.TextInput(
        label="Note/highlight (optional)", style=discord.TextStyle.paragraph,
        required=False, max_length=300,
    )
    media_link = discord.ui.TextInput(
        label="Media link (YouTube/Twitch/image)", required=False, max_length=300,
    )

    def __init__(self, cog: "RaidSummaryCog", tier: str, report_code: str, clear_status: str, raid_type: str):
        super().__init__()
        self.cog = cog
        self.tier = tier
        self.clear_status = clear_status
        self.raid_type = raid_type
        if report_code:
            self.report_link.default = report_code

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = str(self.report_link).strip()
        if not report:
            await interaction.followup.send(
                "No report was picked or pasted - run /raidsummary again and either pick one from "
                "the dropdown or paste a report link/code in this modal.",
                ephemeral=True,
            )
            return
        await self.cog._create_summary(
            interaction, self.tier, report, self.clear_status, self.raid_type,
            str(self.gargul_export_text).strip() or None,
            str(self.media_link).strip() or None,
            str(self.note).strip() or None,
        )


class RaidSummaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.forum_channel_id = int(os.environ["RAID_SUMMARY_FORUM_CHANNEL_ID"])
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.server_slug = os.environ["SERVER_SLUG"]
        self.server_region = os.environ.get("SERVER_REGION", "us")
        self.guild_name = os.environ.get("GUILD_NAME")  # optional - enables the guild-rank section
        logs_channel_id = os.environ.get("RAID_LOGS_CHANNEL_ID")
        self.logs_channel_id = int(logs_channel_id) if logs_channel_id else None  # optional - enables the report picker
        self._bulk_tasks = []  # keeps /raidsummary-bulk's background task(s) alive - see raidsummary_bulk

        # Own dedicated JSON store, separate from the shared bot.store
        # (applications.json) every other cog uses - same "one small file
        # per subsystem" pattern wcl_client.py (wcl_report_cache.json) and
        # wowhead.py (wowhead_item_cache.json) already use, rather than
        # mixing raid-summary's kill/clear/parse/uptime/tier-stat records
        # and per-message Edit/Add-Loot data into the same file as
        # applications/attendance/announcements.
        self.store = ApplicationStore(path="raid_summary_store.json")

    async def cog_load(self):
        # Registers both buttons' custom_ids so they keep working across
        # bot restarts - same pattern as announcements.py's draft/published
        # Edit button. The actual record lookup happens by the clicked
        # message's ID at click time, not anything baked into this dummy view.
        dummy = discord.ui.LayoutView(timeout=None)
        self._add_action_buttons(dummy)
        self.bot.add_view(dummy)

    # --- permission check (self-contained, same as other cogs) -----------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    # --- kill/clear-time/parse records -------------------------------------

    def _get_records(self) -> dict:
        record = self.store.get(RECORDS_KEY)
        if record is None:
            return {"encounters": {}, "clears": {}, "parses": {}, "buffs": {}, "tier_stats": {}}
        return {
            "encounters": record.get("encounters", {}),
            "clears": record.get("clears", {}),
            "parses": record.get("parses", {}),
            "buffs": record.get("buffs", {}),
            "tier_stats": record.get("tier_stats", {}),
        }

    def _save_records(self, records: dict):
        self.store.set(
            RECORDS_KEY, encounters=records["encounters"], clears=records["clears"],
            parses=records["parses"], buffs=records["buffs"], tier_stats=records["tier_stats"],
        )

    def _normalize_tier_reports(self, stored: dict) -> list:
        """
        Returns the [{"code", "raid_type"}, ...] list from a tier-reports
        store entry, transparently upgrading the legacy flat {"codes":
        [...]} shape (written before raid_type tracking existed - a bare
        list of report codes with no per-entry raid_type) into the current
        one. Every report recorded under that old shape was necessarily
        "main" - it predates /raidsummary-bulk gaining a raid_type option
        at all, and every raid summary posted before then went through the
        interactive flow's own main/alt picker, but only the FIRST bulk
        import run (before this method existed) is actually known to have
        hit this path, and that one was confirmed main-raid.

        Used by both _record_tier_report (write) and get_tier_reports
        (read) so a write against legacy data upgrades the stored shape
        in place instead of silently discarding it - self._get(key) or
        {"reports": []}) followed by a bare .get("reports", []) would
        instead see no "reports" key on legacy data, start a fresh empty
        list, and overwrite (not merge into) the stored value on the next
        save, permanently losing every code recorded before the shape
        changed.
        """
        if "reports" in stored:
            return list(stored["reports"])
        return [{"code": c, "raid_type": "main"} for c in stored.get("codes", [])]

    def _record_tier_report(self, tier_name: str, report_code: str, raid_type: str):
        """
        Appends {code, raid_type} to the running list of every report ever
        successfully posted under this tier (deduplicated by code,
        insertion order preserved) - separate from RECORDS_KEY since this
        isn't a "best value on record" like everything else there, just a
        plain roster. raid_type is kept alongside the code so a reader can
        filter to "main" only - an interactively-posted alt/fun raid for
        the same tier shouldn't count toward tier-wide records like
        fastest clear, attendance, or the unique-roster count. Exists so
        the end-of-tier retrospective (cogs/tier_retrospective.py) can read
        the exact report list back later without it being re-supplied, and
        iterate it straight from wcl_report_cache.json (every report here
        was already fully fetched at post time) with no new WCL calls
        needed - see get_tier_reports().
        """
        key = f"{TIER_REPORTS_KEY_PREFIX}{tier_name}"
        reports = self._normalize_tier_reports(self.store.get(key) or {})
        if not any(r.get("code") == report_code for r in reports):
            reports.append({"code": report_code, "raid_type": raid_type})
        self.store.set(key, reports=reports)

    def get_tier_reports(self, tier_name: str, raid_type: str = "main") -> list:
        """
        Public accessor (used cross-cog by cogs/tier_retrospective.py) -
        returns the list of report codes ever successfully posted under
        this tier, in the order they were posted. raid_type defaults to
        "main" since tier-wide records (fastest clear, attendance, unique
        roster) shouldn't include alt/fun raids - pass None for every
        report regardless of type.
        """
        key = f"{TIER_REPORTS_KEY_PREFIX}{tier_name}"
        reports = self._normalize_tier_reports(self.store.get(key) or {})
        if raid_type is not None:
            reports = [r for r in reports if r.get("raid_type") == raid_type]
        return [r["code"] for r in reports]

    def get_tier_report_entries(self, tier_name: str, raid_type: str = "main") -> list:
        """
        Like get_tier_reports(), but returns the full {"code", "raid_type",
        "session_id"} entries instead of just bare codes - used by
        cogs/tier_retrospective.py's aggregation to group reports into
        "sessions" (raid weeks), which a plain code list can't carry.
        session_id defaults to a report's own code (i.e. every report is
        its own session, one week each - today's behavior) unless
        merge_tier_reports() folded two or more codes into a shared one -
        see that method's docstring for why that's needed at all (a raid
        week split across multiple calendar nights).
        """
        key = f"{TIER_REPORTS_KEY_PREFIX}{tier_name}"
        reports = self._normalize_tier_reports(self.store.get(key) or {})
        if raid_type is not None:
            reports = [r for r in reports if r.get("raid_type") == raid_type]
        return [
            {"code": r["code"], "raid_type": r.get("raid_type"), "session_id": r.get("session_id") or r["code"]}
            for r in reports
        ]

    def merge_tier_reports(self, tier_name: str, codes: list) -> bool:
        """
        Folds 2+ already-recorded report codes into one shared session_id,
        so cogs/tier_retrospective.py's aggregation treats them as ONE raid
        week - one week number, and "fastest raid night" summed across all
        of them - instead of one week per report. For when a guild splits
        a raid week across multiple calendar nights (e.g. SSC cleared one
        night, TK cleared a different night the same week), which the
        default one-report-per-week numbering gets wrong.

        Every OTHER stat (medals, damage/healing/deaths, potions,
        attendance, unique roster, per-instance fastest-clear) is
        unaffected by merging - those are still computed per individual
        report exactly as before. Only week numbering and the raid-night-
        duration stat change.

        Returns False (no change made) if any given code isn't currently
        on record for this tier, or fewer than 2 codes were given -
        callers should treat that as a usage error, not a silent no-op
        success. The session_id itself is an opaque deterministic string
        (sorted, joined codes) - nothing outside this store reads it.
        """
        if len(codes) < 2:
            return False
        key = f"{TIER_REPORTS_KEY_PREFIX}{tier_name}"
        reports = self._normalize_tier_reports(self.store.get(key) or {})
        by_code = {r["code"]: r for r in reports}
        if any(c not in by_code for c in codes):
            return False
        session_id = "session:" + "+".join(sorted(codes))
        for c in codes:
            by_code[c]["session_id"] = session_id
        self.store.set(key, reports=reports)
        return True

    # --- tier / banner resolution -------------------------------------------

    def _resolve_tier(self, tier_name: str) -> dict:
        for tier in (config.CURRENT_TIER, config.PREVIOUS_TIER):
            if tier["name"] == tier_name:
                return tier
        raise ValueError(f"Unknown tier '{tier_name}'.")

    def _tier_clear_groupings(self, tier: dict) -> dict:
        """{instance display name: [boss names]} - config.TIER_SUB_INSTANCES'
        breakdown for this tier if configured, otherwise the whole tier
        treated as one single instance under its own name."""
        sub_instances = config.TIER_SUB_INSTANCES.get(tier["name"])
        if sub_instances:
            return sub_instances
        return {tier["name"]: list(tier["bosses"].keys())}

    def _load_banner(self, tier_name: str):
        """Returns (media_source, file_or_None) for the tier's banner, per
        config.RAID_TIER_BANNERS. A plain http(s) URL is used as-is; a local
        path is attached to the message and referenced via attachment://
        (see the module docstring). A missing/unreadable local file just
        means no banner - logged, never raises."""
        path = config.RAID_TIER_BANNERS.get(tier_name)
        if not path:
            return None, None
        if path.startswith("http://") or path.startswith("https://"):
            return path, None
        if not os.path.isfile(path):
            log.warning("Configured banner for tier '%s' not found: %s", tier_name, path)
            return None, None
        filename = os.path.basename(path)
        return f"attachment://{filename}", discord.File(path, filename=filename)

    def _resolve_applied_tags(self, forum_channel: discord.ForumChannel, tier_name: str,
                               clear_status_value: str, raid_type_value: str) -> list:
        """
        Tier and raid-type tags are matched by exact Discord tag ID
        (config.TIER_TAG_IDS / RAID_TYPE_TAG_IDS); the clear-status tag is
        matched by name (config.CLEAR_STATUS_TAG_NAMES - no fixed ID was
        given for those). Either way, a tag that isn't actually present in
        this forum channel right now is silently skipped rather than
        failing the post - see the comment above CLEAR_STATUS_TAG_NAMES.
        """
        available_by_id = {t.id: t for t in forum_channel.available_tags}
        wanted_ids = []
        if tier_name in config.TIER_TAG_IDS:
            wanted_ids.append(config.TIER_TAG_IDS[tier_name])
        if raid_type_value in config.RAID_TYPE_TAG_IDS:
            wanted_ids.append(config.RAID_TYPE_TAG_IDS[raid_type_value])
        applied = [available_by_id[tid] for tid in wanted_ids if tid in available_by_id]

        clear_status_name = config.CLEAR_STATUS_TAG_NAMES.get(clear_status_value, "").lower()
        applied += [
            t for t in forum_channel.available_tags
            if t.name.lower() == clear_status_name and t.id not in wanted_ids
        ]
        return applied

    def _name_icon(self, guild: discord.Guild, class_name) -> str:
        """A class icon (app emoji, same source as /checkattendance's
        roster) prefix for a name, or '' if the class is unknown/unresolved -
        never raises, a missing icon just means the plain name."""
        if not class_name:
            return ""
        icon = icons.resolve_class_icon(guild, class_name)
        return f"{icon} " if icon else ""

    async def _fetch_recent_log_entries(self) -> list:
        """
        Scans the most recent messages in the configured #logs channel
        (RAID_LOGS_CHANNEL_ID) for WCL report links posted there by a
        third-party webhook/app (e.g. "Crusader's Logs"), so
        RaidSummaryOptionsView can offer them as a dropdown labeled by
        their report title instead of the moderator hunting down and
        pasting the raw link. Best-effort like everything else optional
        here: an unconfigured/inaccessible channel, or one with no
        matching embeds, just means an empty list - the modal's manual
        report-link field still works either way. Capped at
        LOGS_SELECT_LIMIT since that's Discord's own per-select-menu
        option limit anyway.

        When cogs/raid_logs.py is loaded and configured, its own structured
        (already-parsed, already-deduplicated) recent-log data is preferred
        over re-scraping embeds here - see get_recent_entries_for_picker.
        Falls through to the channel-scrape below if that cog isn't loaded,
        so this still works standalone.
        """
        raid_logs_cog = self.bot.get_cog("RaidLogsCog")
        if raid_logs_cog is not None:
            entries = raid_logs_cog.get_recent_entries_for_picker(limit=LOGS_SELECT_LIMIT)
            if entries:
                return [
                    {
                        "label": e["label"],
                        "report_code": e["report_code"],
                        "created_at": e["created_at"],
                    }
                    for e in entries
                ]

        if not self.logs_channel_id:
            return []
        channel = self.bot.get_channel(self.logs_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.logs_channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.warning("RAID_LOGS_CHANNEL_ID set but channel not found/visible")
                return []

        entries = []
        try:
            async for message in channel.history(limit=LOGS_HISTORY_LIMIT):
                for embed in message.embeds:
                    report_code = _extract_log_report_code(embed)
                    if not report_code:
                        continue
                    label = (embed.description or embed.title or f"Report {report_code}").strip()
                    entries.append({
                        "label": label[:100],
                        "report_code": report_code,
                        "created_at": message.created_at,
                    })
                    break  # one entry per message is enough
                if len(entries) >= LOGS_SELECT_LIMIT:
                    break
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Couldn't read #logs channel history for the report picker", exc_info=True)
            return []
        return entries

    # --- data assembly -----------------------------------------------------

    async def _resolve_loot(self, loot_rows: list) -> list:
        """Attaches resolved Wowhead item data to each Gargul loot row."""
        item_cache = {}
        resolved = []
        for row in loot_rows:
            item_id = row["item_id"]
            if item_id not in item_cache:
                item_cache[item_id] = await self.bot.wowhead.get_item(item_id)
            resolved.append({**row, "item": item_cache[item_id]})
        return resolved

    async def _fetch_existing_app_emojis(self) -> dict:
        """Fetched ONCE per command run and passed to every icon-provisioning
        call in that run (loot, potions, buff/debuff uptime) - see
        icons.ensure_item_emoji/ensure_spell_emoji's own docstring for why
        this needs to be shared rather than re-fetched per icon."""
        try:
            return {e.name: e for e in await self.bot.fetch_application_emojis()}
        except Exception:
            log.warning("Couldn't fetch application emojis for icon provisioning", exc_info=True)
            return {}

    async def _build_loot_lines(self, resolved_loot: list, guild: discord.Guild, classes_map: dict,
                                 existing_by_name: dict) -> list:
        """
        One compact line per loot row: "<item icon> [Item](wowhead) →
        <class icon> **Winner** *(OS)*". The item icon is a real bot-owned
        emoji for that item, provisioned on demand and cached forever (see
        icons.ensure_item_emoji) - reuses the exact same "download the icon,
        upload as an application emoji" mechanism /add-emoji already uses,
        just triggered automatically per unique item in this loot list
        instead of by a moderator pasting links. Falls back to a colored
        quality-square emoji if an item's icon couldn't be provisioned. The
        winner's class icon comes from classes_map (see
        wcl_client.get_report_role_composition) - same icon source as
        /checkattendance's roster.
        """
        if not resolved_loot:
            return []

        icon_cache = {}
        lines = []
        for row in resolved_loot:
            item = row["item"]
            item_id = row["item_id"]
            if item_id not in icon_cache:
                icon = ""
                if item.get("icon_url"):
                    icon = await icons.ensure_item_emoji(self.bot, existing_by_name, item_id, item["icon_url"])
                    await asyncio.sleep(0.3)  # pacing - a big loot night can provision many new emoji in a row
                icon_cache[item_id] = icon or QUALITY_EMOJI.get(item.get("quality"), DEFAULT_QUALITY_EMOJI)

            offspec_tag = " *(OS)*" if row["offspec"] else ""
            class_icon = self._name_icon(guild, classes_map.get(row["character"]))
            lines.append(
                f"{icon_cache[item_id]} [{item['name']}]({item['wowhead_url']}) → "
                f"{class_icon}**{row['character']}**{offspec_tag}"
            )
        return lines

    def _group_fights_by_encounter(self, fights: list) -> dict:
        groups = {}
        for fight in fights:
            encounter_id = fight.get("encounter_id")
            if encounter_id is None:
                continue  # trash/non-encounter fights don't count toward boss pulls
            if not fight["kill"] and (fight.get("boss_percentage") or 0) >= 100:
                continue  # a 100%-HP "wipe" is a deliberate reset, not a real pull
            groups.setdefault(encounter_id, []).append(fight)
        return groups

    def _tier_stats(self, tier: dict, fights_by_encounter: dict) -> tuple:
        """Returns (killed_count, attempted_count, total_pulls) - scoped to
        ONLY this tier's own configured bosses. fights_by_encounter can
        contain fights for encounter_ids outside the tier (e.g. a stray
        unrelated encounter in the same report) - those must not count
        toward this tier's stats, which is exactly what iterating
        tier["bosses"] (rather than fights_by_encounter directly) ensures -
        same scoping _build_boss_lines already uses for its own listing."""
        killed_count = attempted_count = total_pulls = 0
        for encounter_id in tier["bosses"].values():
            group = fights_by_encounter.get(encounter_id)
            if not group:
                continue
            attempted_count += 1
            total_pulls += len(group)
            if any(f["kill"] for f in group):
                killed_count += 1
        return killed_count, attempted_count, total_pulls

    def _build_boss_lines(self, tier: dict, fights_by_encounter: dict, records: dict, report_code: str) -> tuple:
        """
        Returns (lines, newly_killed_ids, new_fastest_kills) where
        new_fastest_kills is {encounter_id: duration_ms} for every boss this
        raid that either had no prior time on record, or beat/tied it - the
        caller persists these after a successful post. Every non-kill
        (wipe) fight in a boss's group gets its own indented line below the
        boss's summary line, in pull order, showing the boss's remaining
        health % at that wipe. "killed"/"Wipe N" are each a link straight
        to that specific pull's Wipefest analysis (see _wipefest_fight_url).
        """
        lines = []
        newly_killed = []
        new_fastest_kills = {}
        encounters = records["encounters"]

        for boss_name, encounter_id in tier["bosses"].items():
            group = fights_by_encounter.get(encounter_id)
            if not group:
                continue  # not attempted this raid - omit rather than clutter the list
            pulls = len(group)
            kill_fight = next((f for f in group if f["kill"]), None)

            if kill_fight:
                duration_ms = (kill_fight["end_time"] or 0) - (kill_fight["start_time"] or 0)
                clock = _format_clock(duration_ms)
                existing = encounters.get(str(encounter_id))
                prior_fastest = existing.get("fastest_ms") if existing else None

                if existing is None:
                    badge = " 🆕 **First kill!**"
                    newly_killed.append(encounter_id)
                    new_fastest_kills[encounter_id] = duration_ms
                elif prior_fastest is None:
                    badge = ""  # boss was killed before but no time was on record yet - just start tracking
                    new_fastest_kills[encounter_id] = duration_ms
                else:
                    delta = duration_ms - prior_fastest
                    if delta < 0:
                        badge = f" ⚡ **Fastest kill!** ({_format_delta(delta)})"
                        new_fastest_kills[encounter_id] = duration_ms
                    elif delta == 0:
                        badge = " ⚡ **Tied our fastest!**"
                        new_fastest_kills[encounter_id] = duration_ms
                    else:
                        badge = f" ({_format_delta(delta)})"

                kill_url = _wipefest_fight_url(report_code, kill_fight["id"])
                lines.append(
                    f"✅ **{boss_name}** — [killed]({kill_url}) in {pulls} pull{'s' if pulls != 1 else ''} "
                    f"({clock}){badge}"
                )
            else:
                lines.append(f"❌ **{boss_name}** — {pulls} pull{'s' if pulls != 1 else ''}, not downed")

            wipe_number = 0
            for f in group:
                if f["kill"]:
                    continue
                wipe_number += 1
                pct = f.get("boss_percentage")
                pct_text = f"{round(pct)}%" if pct is not None else "?"
                wipe_url = _wipefest_fight_url(report_code, f["id"])
                lines.append(f" ↳ [Wipe {wipe_number}]({wipe_url}): {pct_text}")

        return lines, newly_killed, new_fastest_kills

    def _build_clear_time_line(self, instance_name: str, clear_ms: int, records: dict) -> tuple:
        """Returns (line, new_fastest_clear_ms_or_None) for one instance."""
        clear_words = _format_duration_words(clear_ms)
        prior = records["clears"].get(instance_name, {}).get("fastest_ms")

        if prior is None:
            return f"🕐 **{instance_name} clear time:** {clear_words}", clear_ms

        delta = clear_ms - prior
        if delta < 0:
            return f"🕐 **{instance_name} clear time:** {clear_words} ⚡ **Fastest clear!** ({_format_delta(delta)})", clear_ms
        elif delta == 0:
            return f"🕐 **{instance_name} clear time:** {clear_words} ⚡ **Tied our fastest!**", clear_ms
        else:
            return f"🕐 **{instance_name} clear time:** {clear_words} ({_format_delta(delta)})", None

    def _build_clear_time_lines(self, tier: dict, fights_by_encounter: dict, records: dict) -> tuple:
        """
        Returns (lines, updates) - one line per instance that was FULLY
        cleared this raid (every configured boss in that instance/tier
        killed), independent of the mod's Full-Clear/Progress pick - a raid
        marked "Progress" overall can still show a clean per-instance clear
        if that specific wing got finished. updates is {instance_name:
        new_fastest_ms} for the caller to persist after a successful post.
        """
        lines, updates = [], {}
        for instance_name, boss_names in self._tier_clear_groupings(tier).items():
            encounter_ids = [tier["bosses"][n] for n in boss_names if n in tier["bosses"]]
            groups = [fights_by_encounter.get(eid) for eid in encounter_ids]
            if not encounter_ids or any(g is None for g in groups):
                continue
            if not all(any(f["kill"] for f in g) for g in groups):
                continue

            times = [
                (f["start_time"], f["end_time"]) for g in groups for f in g
                if f["start_time"] is not None and f["end_time"] is not None
            ]
            if not times:
                continue
            duration_ms = max(t[1] for t in times) - min(t[0] for t in times)

            line, new_fastest = self._build_clear_time_line(instance_name, duration_ms, records)
            lines.append(line)
            if new_fastest is not None:
                updates[instance_name] = new_fastest
        return lines, updates

    def _build_comp_block(self, composition: dict) -> str:
        """
        Raid composition via a 70%-threshold role classification (see
        wcl_client.ROLE_THRESHOLD / get_report_role_composition) rather than
        each character's single most-common role - a tank/healer who also
        DPS'd some fights still counts as their main role as long as they
        filled it at least 70% of the time; otherwise they're counted as
        DPS, same as WCL's own comp breakdown handles hybrids. `composition`
        is fetched ONCE by the caller (a separate, lazily-cached WCL call,
        not part of the always-cheap report summary - the one fetch that
        can still take a moment on a report with a lot of wipes) and reused
        for the class icons everywhere else in the summary too.
        """
        if not composition:
            return ""
        total = len(composition["tanks"]) + len(composition["healers"]) + len(composition["dps"])
        if not total:
            return ""
        return (
            f"**Roster:** {total} raiders — {len(composition['tanks'])} Tanks, "
            f"{len(composition['healers'])} Healers, {len(composition['dps'])} DPS"
        )

    def _check_stat_record(self, records: dict, tier_name: str, stat_key: str, direction: str,
                            value: float, delta_fmt) -> tuple:
        """
        Generic "raid record" check for a top-N leaderboard's #1 entry,
        scoped per tier (records["tier_stats"][tier_name][stat_key]) - a
        number that would be a record in one tier isn't comparable to
        another, same reasoning the kill-time/clear-time records are
        already scoped to a specific boss/instance rather than kept raid-
        wide. direction is "high" (bigger is better - damage, healing,
        activity%, potions, interrupts, dispels, deaths) or "low" (smaller
        is better - overheal%).

        Returns (badge_text, update_or_None) - update is what the caller
        persists into records["tier_stats"][tier_name][stat_key] after a
        successful post (None if this raid's #1 didn't beat the record, or
        there wasn't one yet to beat - mirrors _build_clear_time_line's "no
        record yet, just start tracking silently, no badge" behavior).
        """
        prior = records["tier_stats"].get(tier_name, {}).get(stat_key)
        if prior is None:
            return "", {"value": value}

        beat = value > prior["value"] if direction == "high" else value < prior["value"]
        if not beat:
            return "", None

        delta = abs(value - prior["value"])
        return f" — 🏆 **New raid record!** (by {delta_fmt(delta)})", {"value": value}

    def _build_ranked_block(self, title: str, icon: str, values: dict, guild: discord.Guild, classes_map: dict,
                             value_fmt, records: dict = None, tier_name: str = None, stat_key: str = None,
                             direction: str = "high", delta_fmt=None, top_n: int = 3) -> tuple:
        """
        Shared top-N leaderboard renderer (medal + class icon + name +
        value_fmt(value) per line) used for every count/percent fun-stat
        and the damage/death/healing/overheal leaderboards - only top_n,
        sort direction, and whether a record is tracked differ per caller.

        When records/tier_name/stat_key are all given, the #1 entry's
        value is checked against this tier's all-time record for that stat
        (see _check_stat_record) and gets a "New raid record!" badge if it
        beats it. delta_fmt defaults to value_fmt if not given - pass a
        plainer one when value_fmt also appends something like "(% of
        raid)" that wouldn't make sense on a bare delta.

        direction "high" sorts descending (top_n BIGGEST values - damage,
        activity%, ...); "low" sorts ascending (top_n SMALLEST - overheal%,
        where being lowest is the achievement) - rank medals always go to
        index 0, i.e. whichever end is the "winning" one for that stat.

        Returns (block_text, record_update_or_None) - update is what the
        caller persists into records["tier_stats"] after a successful
        post, same pattern as every other record type in this file.
        """
        if not values:
            return "", None
        top = sorted(values.items(), key=lambda kv: kv[1], reverse=(direction != "low"))[:top_n]
        update = None
        lines = []
        for i, (name, value) in enumerate(top):
            badge = ""
            if i == 0 and records is not None and stat_key:
                badge, update = self._check_stat_record(
                    records, tier_name, stat_key, direction, value, delta_fmt or value_fmt
                )
            lines.append(
                f"{RANK_MEDALS[i]} {self._name_icon(guild, classes_map.get(name))}**{name}** — {value_fmt(value)}{badge}"
            )
        return f"{icon} **{title}**\n" + "\n".join(lines), update

    def _build_deaths_block(self, deaths: dict, guild: discord.Guild, classes_map: dict,
                             records: dict, tier_name: str) -> tuple:
        if not deaths:
            return "", None
        total = sum(deaths.values())
        title = f"Death's Leaderboard — {total} total death{'s' if total != 1 else ''}"
        return self._build_ranked_block(
            title, "💀", deaths, guild, classes_map,
            lambda v: f"{int(v)} death{'s' if int(v) != 1 else ''}",
            records=records, tier_name=tier_name, stat_key="most_deaths", direction="high",
            delta_fmt=lambda v: f"{int(round(v))}",
        )

    def _build_damage_block(self, damage_done: dict, guild: discord.Guild, classes_map: dict,
                             records: dict, tier_name: str) -> tuple:
        """Top 3 by total damage across the whole report (bosses AND
        trash) - matches WCL's own "Overall" damage-done ranking view,
        including each entry's exact share of the raid's total damage."""
        if not damage_done:
            return "", None
        total_damage = sum(damage_done.values()) or 1
        return self._build_ranked_block(
            "Top Overall Damage (bosses + trash)", "⚔️", damage_done, guild, classes_map,
            lambda v: f"{int(v):,} damage ({v / total_damage * 100:.2f}%)",
            records=records, tier_name=tier_name, stat_key="damage_done", direction="high",
            delta_fmt=lambda v: f"{int(round(v)):,} damage",
        )

    def _build_healing_block(self, healing_done: dict, guild: discord.Guild, classes_map: dict,
                              records: dict, tier_name: str) -> tuple:
        """Top 3 by total healing done across the whole report (bosses AND
        trash) - same scope/style as _build_damage_block's damage version."""
        if not healing_done:
            return "", None
        total_healing = sum(healing_done.values()) or 1
        return self._build_ranked_block(
            "Top Healing Done (bosses + trash)", "💚", healing_done, guild, classes_map,
            lambda v: f"{int(v):,} healing ({v / total_healing * 100:.2f}%)",
            records=records, tier_name=tier_name, stat_key="healing_done", direction="high",
            delta_fmt=lambda v: f"{int(round(v)):,} healing",
        )

    def _build_overheal_block(self, overheal_pct: dict, healer_names: set, guild: discord.Guild,
                               classes_map: dict, records: dict, tier_name: str) -> tuple:
        """Top 3 LOWEST overheal % (bosses+trash) among raiders who filled
        a healer role this raid (composition["healers"] - see
        wcl_client.get_report_role_composition) - restricted to healers so
        a DPS's single incidental self-heal doesn't show up as a
        misleadingly "perfect" 0%-overheal leader on a tiny sample size."""
        filtered = {name: pct for name, pct in overheal_pct.items() if name in healer_names}
        if not filtered:
            return "", None
        return self._build_ranked_block(
            "Least Overhealed % (bosses + trash)", "💧", filtered, guild, classes_map,
            lambda v: f"{v:.1f}%",
            records=records, tier_name=tier_name, stat_key="overheal_pct_lowest", direction="low",
            delta_fmt=lambda v: f"{v:.1f}%",
        )

    async def _resolve_spell_icon(self, spell_id: int, existing_by_name: dict) -> str:
        """One-off spell-icon resolution for a leaderboard header that
        isn't tied to per-line WCL matching (Top Interrupters/Dispellers) -
        same provisioning mechanism _build_uptime_lines uses per tracked
        ability, just for a single decorative icon instead."""
        if not spell_id:
            return ""
        spell = await self.bot.wowhead.get_spell(spell_id)
        if not spell.get("icon_url"):
            return ""
        return await icons.ensure_spell_emoji(self.bot, existing_by_name, spell_id, spell["icon_url"])

    async def _build_potions_block(self, potion_casts: dict, guild: discord.Guild, classes_map: dict,
                                    existing_by_name: dict, records: dict, tier_name: str) -> tuple:
        """Top-3 "Destruction + Haste potions used" leaderboard (config.
        TRACKED_POTION_BUFF_SPELL_IDS, combined into one count per player by
        wcl_client._fetch_buff_usage_by_player - tracked via the temporary
        buff each potion grants, not a Casts-table cast, since a potion's
        own use never appears there by name) - headed by both potions'
        real Wowhead item icons (config.TRACKED_POTION_ITEM_IDS) instead of
        a generic emoji, same provisioning mechanism loot icons use."""
        if not potion_casts:
            return "", None

        icon_prefix = ""
        for item_id in config.TRACKED_POTION_ITEM_IDS.values():
            item = await self.bot.wowhead.get_item(item_id)
            if item.get("icon_url"):
                icon_prefix += await icons.ensure_item_emoji(self.bot, existing_by_name, item_id, item["icon_url"])
        return self._build_ranked_block(
            "Top Potion Users (Destruction + Haste)", icon_prefix or "🧪", potion_casts, guild, classes_map,
            lambda v: f"{int(v)} used",
            records=records, tier_name=tier_name, stat_key="potions_used", direction="high",
            delta_fmt=lambda v: f"{int(round(v))}",
        )

    async def _build_uptime_lines(self, aura_uptime: dict, records: dict, tier_name: str, guild: discord.Guild,
                                   classes_map: dict, existing_by_name: dict) -> tuple:
        """
        Returns (lines, updates) for the "Buff/Debuff Uptime" section -
        config.TRACKED_DEBUFFS (kept on the boss) and config.TRACKED_BUFFS
        (kept on a player), each shown with an "all fights" (bosses+trash)
        and a "boss fights only" uptime %, plus whichever raider contributed
        the most of the boss-fight uptime (see
        wcl_client.get_report_aura_uptime). An ability that never appeared
        at all this raid (both percentages None) is omitted, same as an
        un-attempted boss in _build_boss_lines.

        The record compared against/updated (records["buffs"][tier_name]) is
        always the BOSS-ONLY percentage - trash uptime is too inconsistent
        raid-to-raid to be a meaningful "personal best" - mirrors how clear-
        time records are boss/instance-scoped, not raid-wide. Scoped per
        tier (same reasoning as _check_stat_record: a boss's own comp/
        strategy differs enough tier-to-tier that a cross-tier "record"
        wouldn't be a fair comparison) - updates is
        {ability_name: new_best_boss_pct} for the caller to persist under
        this tier after a successful post, same pattern as
        _build_clear_time_lines.
        """
        if not aura_uptime:
            return [], {}

        stored = records["buffs"].get(tier_name, {})
        lines, updates = [], {}
        for name in config.TRACKED_DEBUFFS + config.TRACKED_BUFFS:
            data = aura_uptime.get(name) or {}
            boss_pct, all_pct = data.get("boss_pct"), data.get("all_pct")
            if boss_pct is None and all_pct is None:
                continue  # never appeared this raid - omit rather than clutter

            icon = ""
            spell_id = config.TRACKED_ABILITY_ICON_SPELL_IDS.get(name)
            if spell_id:
                spell = await self.bot.wowhead.get_spell(spell_id)
                if spell.get("icon_url"):
                    icon = await icons.ensure_spell_emoji(self.bot, existing_by_name, spell_id, spell["icon_url"])
                    await asyncio.sleep(0.3)  # same pacing _build_loot_lines uses for item-icon provisioning

            badge = ""
            if boss_pct is not None:
                prior = stored.get(name, {}).get("best_uptime_pct")
                if prior is None:
                    badge = " 🆕 **First time tracked**"
                    updates[name] = boss_pct
                elif boss_pct > prior:
                    badge = f" ⚡ **New best!** (+{boss_pct - prior:.1f}%)"
                    updates[name] = boss_pct
                elif boss_pct < prior:
                    badge = f" ({boss_pct - prior:+.1f}%)"

            top_bit = ""
            if data.get("top_player"):
                top_icon = self._name_icon(guild, classes_map.get(data["top_player"]))
                top_pct = data.get("top_player_pct")
                top_pct_text = f" ({top_pct:.1f}%)" if top_pct is not None else ""
                top_bit = f" — best kept by {top_icon}**{data['top_player']}**{top_pct_text}"

            boss_text = f"{boss_pct:.1f}%" if boss_pct is not None else "?"
            all_text = f"{all_pct:.1f}%" if all_pct is not None else "?"
            lines.append(
                f"{icon} **{name}** — {boss_text} bosses / {all_text} all fights{badge}{top_bit}"
            )
        return lines, updates

    def _build_parses_block(self, parses: list, fights: list, damage_done: dict, healing_done: dict,
                             guild: discord.Guild, classes_map: dict, personal_best_updates: dict) -> str:
        """
        "Raid MVP's" - three separate lines: highest AVERAGE DPS parse
        across all of this raid's boss kills (matches WCL's own "avg"
        rankings column - DPS-role parses only, since healers/tanks use
        their own separate WCL metrics, not comparable on the same
        percentile scale), highest overall damage done, and highest
        overall healing done - the latter two each also show that
        amount's share of the raid's total damage/healing, same % done
        in _build_damage_block. Followed by "Noteworthy parses" - the elite
        (>= config.PARSE_HIGHLIGHT_THRESHOLD) individual-boss parse
        callouts, any role, tagged as a personal best where applicable
        (personal_best_updates - see _build_personal_bests_block, computed
        by the caller BEFORE this so that info is available here too).
        """
        fight_names = {f["id"]: f["name"] for f in fights}
        fight_encounter = {f["id"]: f.get("encounter_id") for f in fights}
        with_pct = [p for p in parses if p.get("rank_percent") is not None]

        mvp_lines = []
        dps_pcts_by_name = {}
        dps_class_by_name = {}
        for p in with_pct:
            if p.get("role") != "dps":
                continue
            dps_pcts_by_name.setdefault(p["name"], []).append(p["rank_percent"])
            dps_class_by_name.setdefault(p["name"], p.get("class"))
        if dps_pcts_by_name:
            name, avg_pct = max(
                ((n, sum(pcts) / len(pcts)) for n, pcts in dps_pcts_by_name.items()),
                key=lambda pair: pair[1],
            )
            mvp_lines.append(
                f"🏆 {self._name_icon(guild, dps_class_by_name.get(name))}**{name}** — "
                f"highest average DPS parse ({avg_pct:.1f}%)"
            )
        if damage_done:
            name, amount = max(damage_done.items(), key=lambda kv: kv[1])
            total_damage = sum(damage_done.values()) or 1
            mvp_lines.append(
                f"🏆 {self._name_icon(guild, classes_map.get(name))}**{name}** — "
                f"highest damage done ({amount:,}, {amount / total_damage * 100:.2f}% of raid total)"
            )
        if healing_done:
            name, amount = max(healing_done.items(), key=lambda kv: kv[1])
            total_healing = sum(healing_done.values()) or 1
            mvp_lines.append(
                f"🏆 {self._name_icon(guild, classes_map.get(name))}**{name}** — "
                f"highest healing done ({amount:,}, {amount / total_healing * 100:.2f}% of raid total)"
            )

        lines = []
        if mvp_lines:
            lines.append("**Raid MVP's**")
            lines.extend(mvp_lines)

        elite = sorted(
            (p for p in with_pct if p["rank_percent"] >= config.PARSE_HIGHLIGHT_THRESHOLD),
            key=lambda p: -p["rank_percent"],
        )
        if elite:
            if lines:
                lines.append("")
            lines.append("**Noteworthy parses**")
            for p in elite[:20]:
                encounter_id = fight_encounter.get(p.get("fight_id"))
                is_pb = personal_best_updates.get(encounter_id, {}).get(p["name"]) == p["rank_percent"]
                pb_tag = " — **Personal best!**" if is_pb else ""
                lines.append(
                    f"🌟 {self._name_icon(guild, p.get('class'))}**{p['name']}** — {p['rank_percent']:.1f}% on "
                    f"{_boss_name(p, fight_names)}{pb_tag}"
                )

        return "\n".join(lines)

    def _build_personal_bests_block(self, parses: list, fights: list, records: dict, guild: discord.Guild) -> tuple:
        """
        Returns (block_text, updates) where updates is
        {encounter_id: {character_name: new_best_percent}} for every parse
        that beat that character's own prior-best on that boss - the caller
        persists these after a successful post, same as the fastest-kill
        records (see _build_boss_lines). A character with no prior-best on
        record just gets their first parse silently recorded as the
        baseline (nothing to compare against yet, so no line shown) -
        mirrors how a boss with no fastest-kill record on file behaves.

        If a character has multiple parses of the same boss in this report
        (rare - e.g. a wipe recovery re-kill), each is compared against the
        best seen SO FAR TONIGHT (not just the stored record), so a double
        improvement in one raid doesn't get reported as two separate jumps
        from the old number.
        """
        fight_encounter = {f["id"]: f.get("encounter_id") for f in fights}
        fight_names = {f["id"]: f["name"] for f in fights}
        stored = records["parses"]
        updates = {}

        lines = []
        for p in parses:
            pct = p.get("rank_percent")
            name = p.get("name")
            fight_id = p.get("fight_id")
            encounter_id = fight_encounter.get(fight_id)
            if pct is None or not name or encounter_id is None:
                continue

            tonight_best = updates.get(encounter_id, {}).get(name)
            prior_best = tonight_best if tonight_best is not None else (
                stored.get(str(encounter_id), {}).get(name, {}).get("best_percent")
            )

            if prior_best is None:
                updates.setdefault(encounter_id, {})[name] = pct
                continue
            if pct > prior_best:
                lines.append(
                    f"📈 {self._name_icon(guild, p.get('class'))}**{name}** — {prior_best:.1f}% → {pct:.1f}% on "
                    f"{_boss_name(p, fight_names)} (+{pct - prior_best:.1f})"
                )
                updates.setdefault(encounter_id, {})[name] = pct

        block = ""
        if lines:
            shown = lines[:20]
            block = "**Personal bests broken tonight**\n" + "\n".join(shown)
            if len(lines) > len(shown):
                block += f"\n… +{len(lines) - len(shown)} more"
        return block, updates

    async def _build_guild_rank_block(self, tier: dict, fights_by_encounter: dict) -> str:
        if not self.guild_name:
            return ""
        try:
            rankings = await self.bot.wcl.get_guild_zone_rankings(
                self.guild_name, self.server_slug, self.server_region, tier["zone_id"]
            )
        except Exception:
            log.warning("Guild zone ranking lookup failed", exc_info=True)
            return ""
        if not rankings:
            return ""

        lines = []
        for pb in rankings.get("per_boss") or []:
            if pb.get("encounter_id") not in fights_by_encounter:
                continue
            bits = []
            if pb.get("server_rank"):
                bits.append(f"server #{pb['server_rank']}")
            if pb.get("region_rank"):
                bits.append(f"region #{pb['region_rank']}")
            if pb.get("speed_percent") is not None:
                bits.append(f"{pb['speed_percent']:.1f}% speed")
            if bits:
                lines.append(f"🏰 **{pb.get('encounter_name') or 'Unknown boss'}** — {', '.join(bits)}")
        if not lines:
            return ""
        return "**Guild ranks this tier**\n" + "\n".join(lines)

    def _report_timing(self, summary: dict) -> dict:
        """
        Returns {"date": 'YYYY-MM-DD' or '?', "duration_line": str} from
        the report's first REAL boss pull, not the report's own recorded
        start time (see module docstring's "First pull" note - those can
        differ by several minutes of trash/travel, and WCL's fight times
        are relative offsets from the report's absolute start). Shared by
        _assemble_and_post_summary (for the tldr's duration line) and the
        bulk-import job (which needs just the date, before that method
        even runs, to build its "Week N - <date>" thread name) - both call
        this against the SAME cached get_report_summary() result, so
        computing it twice costs nothing extra.
        """
        report_date = "?"
        duration_line = ""
        encounter_fights = [f for f in summary["fights"] if f.get("encounter_id") is not None]
        first_pull_relative_ms = min(
            (f["start_time"] for f in encounter_fights if f["start_time"] is not None), default=None
        )
        if summary["start_time"] and summary["end_time"] and first_pull_relative_ms is not None:
            first_pull_absolute_ms = summary["start_time"] + first_pull_relative_ms
            raid_duration_ms = summary["end_time"] - first_pull_absolute_ms
            report_date = datetime.fromtimestamp(first_pull_absolute_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            first_pull_clock = (
                datetime.fromtimestamp(first_pull_absolute_ms / 1000, tz=timezone.utc).astimezone(AMSTERDAM_TZ).strftime("%H:%M")
            )
            raid_end_clock = (
                datetime.fromtimestamp(summary["end_time"] / 1000, tz=timezone.utc).astimezone(AMSTERDAM_TZ).strftime("%H:%M")
            )
            duration_line = (
                f"🕐 First pull: **{first_pull_clock}** · Raid ended: **{raid_end_clock}** · "
                f"Total duration: **{_format_duration_compact(raid_duration_ms)}**"
            )
        return {"date": report_date, "duration_line": duration_line}

    def _build_links_block(self, report_code: str) -> str:
        return (
            "**Links**\n"
            f"📜 [Full log](https://fresh.warcraftlogs.com/reports/{report_code})\n"
            f"📊 [Wipefest analysis](https://www.wipefest.gg/report/{report_code}?gameVersion=warcraft-fresh)"
        )

    # --- tldr / footer text (rebuilt fresh on every edit, see module docstring) --

    def _build_tldr_text(self, ctx: dict, note: str) -> str:
        loot_clause = (
            "loot pending" if ctx.get("loot_pending")
            else f"**{ctx['loot_count']}** item{'s' if ctx['loot_count'] != 1 else ''} awarded"
        )
        text = (
            f"## {ctx['clear_label']} — {ctx['tier_name']} ({ctx['report_date']})\n"
            f"**{ctx['killed_count']}/{ctx['attempted_count']}** bosses downed this raid "
            f"({ctx['total_pulls']} total pulls) · {loot_clause}"
        )
        if ctx.get("duration_line"):
            text += f"\n{ctx['duration_line']}"
        for line in ctx.get("clear_time_lines") or []:
            text += f"\n{line}"
        if note:
            text += f"\n\n*{note.strip()}*"
        return text

    def _build_footer_text(self, media_link: str) -> str:
        return media_link.strip() if media_link else ""

    # --- block building / pagination ---------------------------------------

    def _text_block(self, content: str) -> dict:
        return {"content": content, "chars": len(content), "units": 2}

    def _chunk_lines(self, lines: list, max_chars: int) -> list:
        """Joins lines into as few chunks as possible, each under
        max_chars - used to keep the loot list compact (several items per
        message instead of one component per item)."""
        chunks, current, current_len = [], [], 0
        for line in lines:
            line_len = len(line) + 1  # +1 for the joining newline
            if current and current_len + line_len > max_chars:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _build_loot_blocks(self, loot_lines: list) -> list:
        if not loot_lines:
            return []
        chunks = self._chunk_lines(loot_lines, MAX_CHARS_PER_PAGE - 20)
        return [
            self._text_block(("**Loot**\n" if i == 0 else "") + chunk)
            for i, chunk in enumerate(chunks)
        ]

    def _paginate_blocks(self, blocks: list) -> list:
        pages = []
        current, chars, units = [], 0, 0
        for block in blocks:
            if current and (chars + block["chars"] > MAX_CHARS_PER_PAGE or units + block["units"] > MAX_UNITS_PER_PAGE):
                pages.append(current)
                current, chars, units = [], 0, 0
            current.append(block)
            chars += block["chars"]
            units += block["units"]
        if current:
            pages.append(current)
        return pages

    def _add_action_buttons(self, view: discord.ui.LayoutView):
        action_row = discord.ui.ActionRow()
        edit_button = discord.ui.Button(
            label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id=EDIT_BUTTON_CUSTOM_ID
        )
        edit_button.callback = self._on_edit_click
        action_row.add_item(edit_button)
        loot_button = discord.ui.Button(
            label="🎁 Add/Update Loot", style=discord.ButtonStyle.secondary, custom_id=ADD_LOOT_BUTTON_CUSTOM_ID
        )
        loot_button.callback = self._on_add_loot_click
        action_row.add_item(loot_button)
        view.add_item(action_row)

    def _render_page(self, blocks: list, banner_source: str = None, with_action_buttons: bool = False) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_color=discord.Color.blurple())

        if banner_source:
            gallery = discord.ui.MediaGallery(discord.MediaGalleryItem(banner_source))
            container.add_item(gallery)
            container.add_item(discord.ui.Separator())

        for i, block in enumerate(blocks):
            container.add_item(discord.ui.TextDisplay(block["content"]))
            if i < len(blocks) - 1:
                container.add_item(discord.ui.Separator())

        view.add_item(container)
        if with_action_buttons:
            self._add_action_buttons(view)
        return view

    def _render_pages(self, pre_loot_blocks: list, loot_blocks: list, header_ctx: dict,
                       note: str, media_link: str, banner_source: str) -> list:
        """Builds the full page list (LayoutViews) from frozen pre-loot/loot
        blocks plus fresh tldr/footer content - used for the initial post
        and every later edit/add-loot, so they never drift apart. Loot (+
        the footer, if any) is always paginated separately from the header
        content, so it always starts on a fresh message rather than sharing
        space with whatever's left on the last header page - normally
        message #2. pre_loot_blocks[0] is always the links block (WCL log +
        Wipefest) - see the command handler, which builds it that way so it
        stays anchored to the top post without needing its own parameter."""
        tldr_block = self._text_block(self._build_tldr_text(header_ctx, note))
        footer_text = self._build_footer_text(media_link)
        tail_extra = [self._text_block(footer_text)] if footer_text else []

        head_pages = self._paginate_blocks([tldr_block] + pre_loot_blocks)
        tail_pages = self._paginate_blocks(loot_blocks + tail_extra) if (loot_blocks or tail_extra) else []
        all_pages = head_pages + tail_pages

        views = []
        for i, page in enumerate(all_pages):
            is_first, is_last = i == 0, i == len(all_pages) - 1
            views.append(
                self._render_page(page, banner_source=banner_source if is_first else None, with_action_buttons=is_last)
            )
        return views

    # --- the command ---------------------------------------------------

    @app_commands.command(name="raidsummary", description="Post a raid summary to the raid-summary forum (moderator only)")
    async def raidsummary(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can post raid summaries.", ephemeral=True)
            return

        forum_channel = self.bot.get_channel(self.forum_channel_id)
        if forum_channel is None or not isinstance(forum_channel, discord.ForumChannel):
            await interaction.response.send_message(
                "RAID_SUMMARY_FORUM_CHANNEL_ID isn't set to a real forum channel the bot can see.",
                ephemeral=True,
            )
            return

        # Every field starts as a dropdown here (RaidSummaryOptionsView) -
        # Continue then opens RaidSummaryCreateModal for the genuinely
        # free-text fields (report link fallback, loot paste, note, media
        # link). See RaidSummaryOptionsView's docstring for why this can't
        # all be one step: modals have no select-menu support.
        await interaction.response.defer(ephemeral=True, thinking=True)
        log_entries = await self._fetch_recent_log_entries()
        await interaction.followup.send(
            "Pick the raid's details, then hit **Continue** for the report link, loot, note, and media link.",
            view=RaidSummaryOptionsView(self, log_entries),
            ephemeral=True,
        )

    async def _create_summary(self, interaction: discord.Interaction, tier: str, report: str,
                               clear_status: str, raid_type: str,
                               gargul_export_text: str, media_link: str, note: str):
        """The actual posting logic, called from RaidSummaryCreateModal.on_submit
        once the modal's free-text fields are in. interaction has already
        been deferred by the modal by this point. Everything from fetching
        the WCL report through posting/committing records is shared with
        the bulk-import job (raidsummary_bulk) via _assemble_and_post_summary
        - this method only owns the interactive-specific bits: the loot-
        paste validation (only reachable from the modal) and surfacing the
        result as an ephemeral followup."""
        forum_channel = self.bot.get_channel(self.forum_channel_id)
        if forum_channel is None or not isinstance(forum_channel, discord.ForumChannel):
            # Already checked once before the modal was shown - re-checked
            # here in case something changed in between (e.g. the channel
            # was deleted while the moderator was filling out the modal).
            await interaction.followup.send(
                "RAID_SUMMARY_FORUM_CHANNEL_ID isn't set to a real forum channel the bot can see.",
                ephemeral=True,
            )
            return

        # --- parse the pasted loot export, if given now ---
        loot_rows = []
        if gargul_export_text is not None:
            if len(gargul_export_text) >= GARGUL_TEXT_MAX_LENGTH:
                # Discord's own input box enforces this ceiling by silently
                # truncating, not by refusing to submit - so hitting it
                # exactly isn't trusted as a complete export. Reject rather
                # than risk quietly posting a cut-off loot list.
                await interaction.followup.send(
                    f"That export is at Discord's {GARGUL_TEXT_MAX_LENGTH}-character paste limit, "
                    "so it may have been cut off. Post the summary without loot, then use the "
                    "🎁 Add/Update Loot button afterward to upload it as a file instead - no size "
                    "limit there.",
                    ephemeral=True,
                )
                return
            try:
                loot_rows = gargul_loot.parse_gargul_export(gargul_export_text)
            except gargul_loot.GargulParseError as e:
                await interaction.followup.send(f"Couldn't read the loot export: {e}", ephemeral=True)
                return
            except Exception:
                log.exception("Failed to parse pasted Gargul export")
                await interaction.followup.send("Couldn't read the loot export text.", ephemeral=True)
                return

        report_code = _extract_report_code(report)
        tier_data = self._resolve_tier(tier)
        result = await self._assemble_and_post_summary(
            interaction.guild, forum_channel, tier_data, report_code, clear_status, raid_type,
            loot_rows, media_link, note,
        )
        if not result["ok"]:
            await interaction.followup.send(result["error"], ephemeral=True)
            return

        await interaction.followup.send(f"Posted: {result['thread'].mention}", ephemeral=True)

    async def _assemble_and_post_summary(self, guild: discord.Guild, forum_channel: discord.ForumChannel,
                                          tier_data: dict, report_code: str, clear_status: str, raid_type: str,
                                          loot_rows: list, media_link: str, note: str,
                                          thread_name_override: str = None) -> dict:
        """
        Shared core of posting a raid summary - everything from fetching
        the WCL report through creating the forum thread and committing
        every record type (kill/clear times, parses, uptime, tier-stat
        leaderboards). Used by both _create_summary (the interactive
        /raidsummary flow) and the bulk-import job (_run_bulk_import),
        which differ only in how they surface progress/errors (an
        ephemeral followup vs a plain channel message, since the bulk
        job's interaction token is long expired by the time later reports
        in the list get processed) and in whether the thread name is auto-
        derived from the report's own date or overridden (bulk import's
        "Week N" naming - see _report_timing).

        Returns {"ok": True, "thread": thread} on success, or
        {"ok": False, "error": "<user-facing message>"} on any EXPECTED
        failure (bad report code, no fights, Discord post failure) -
        never raises for those, so both callers can turn the same message
        into a followup or a channel post without duplicating error text.
        """
        try:
            summary = await self.bot.wcl.get_report_summary(report_code)
        except Exception:
            log.exception("Failed to fetch WCL report summary for %s", report_code)
            return {"ok": False, "error": f"Couldn't fetch WCL report `{report_code}` - check the link/code and try again."}
        if not summary.get("fights"):
            return {"ok": False, "error": f"WCL report `{report_code}` has no fights - double check the link."}

        resolved_loot = await self._resolve_loot(loot_rows)

        # Raid composition (also the source of class icons used throughout
        # the rest of the summary, including loot) - one lazily-cached WCL
        # call, best-effort like everything else here.
        try:
            composition = await self.bot.wcl.get_report_role_composition(report_code)
        except Exception:
            log.warning("Role composition lookup failed for %s", report_code, exc_info=True)
            composition = None
        classes_map = (composition or {}).get("classes", {})

        fights_by_encounter = self._group_fights_by_encounter(summary["fights"])
        records = self._get_records()
        boss_lines, newly_killed_ids, new_fastest_kills = self._build_boss_lines(
            tier_data, fights_by_encounter, records, report_code
        )
        killed_count, attempted_count, total_pulls = self._tier_stats(tier_data, fights_by_encounter)
        clear_label = "Full Clear!" if clear_status == "full_clear" else "Progress Raid"

        timing = self._report_timing(summary)
        report_date, duration_line = timing["date"], timing["duration_line"]

        clear_time_lines, clear_time_updates = self._build_clear_time_lines(tier_data, fights_by_encounter, records)

        # Boss-fight IDs (this tier's configured bosses only, excluding trash
        # AND the deliberate-reset wipes _group_fights_by_encounter already
        # filtered out) - same scoping _tier_stats/_build_boss_lines use,
        # needed here so the uptime section can show a "boss fights only"
        # number alongside the "all fights" one - see get_report_aura_uptime.
        boss_fight_ids = [
            f["id"] for encounter_id in tier_data["bosses"].values()
            for f in fights_by_encounter.get(encounter_id, [])
        ]
        try:
            aura_uptime = await self.bot.wcl.get_report_aura_uptime(report_code, boss_fight_ids)
        except Exception:
            log.warning("Aura uptime lookup failed for %s", report_code, exc_info=True)
            aura_uptime = {}

        # Not used by anything rendered in THIS summary - fetched purely to
        # warm the per-report cache ahead of a planned end-of-tier
        # retrospective (bosses-only damage/healing/overheal summed across
        # a whole tier's raids). Doing this now, on every post, means that
        # future feature reads entirely from cache with zero new WCL calls
        # instead of needing to re-fetch every already-posted report - see
        # get_report_boss_only_totals's docstring.
        try:
            await self.bot.wcl.get_report_boss_only_totals(report_code, boss_fight_ids)
        except Exception:
            log.warning("Boss-only totals lookup failed for %s", report_code, exc_info=True)

        # Fetched once and reused for every icon this post provisions (loot,
        # potions, buff/debuff uptime) - see _fetch_existing_app_emojis.
        existing_by_name = await self._fetch_existing_app_emojis()

        header_ctx = {
            "clear_label": clear_label, "tier_name": tier_data["name"], "report_date": report_date,
            "killed_count": killed_count, "attempted_count": attempted_count, "total_pulls": total_pulls,
            "loot_count": len(resolved_loot), "loot_pending": not loot_rows,
            "duration_line": duration_line, "clear_time_lines": clear_time_lines,
        }

        pre_loot_blocks = [self._text_block(self._build_links_block(report_code))]

        comp_block = self._build_comp_block(composition)
        if comp_block:
            pre_loot_blocks.append(self._text_block(comp_block))
        if boss_lines:
            pre_loot_blocks.append(self._text_block("**Boss-by-boss**\n" + "\n".join(boss_lines)))
        # Computed before _build_parses_block, which tags a "Noteworthy
        # parse" as a personal best using this same data.
        personal_bests_block, parse_updates = self._build_personal_bests_block(
            summary["parses"], summary["fights"], records, guild
        )
        parses_block = self._build_parses_block(
            summary["parses"], summary["fights"], summary["damage_done"], summary["healing_done"],
            guild, classes_map, parse_updates,
        )
        if parses_block:
            pre_loot_blocks.append(self._text_block(parses_block))
        if personal_bests_block:
            pre_loot_blocks.append(self._text_block(personal_bests_block))
        guild_rank_block = await self._build_guild_rank_block(tier_data, fights_by_encounter)
        if guild_rank_block:
            pre_loot_blocks.append(self._text_block(guild_rank_block))
        tier_name = tier_data["name"]
        healer_names = set((composition or {}).get("healers") or [])

        damage_block, damage_record_update = self._build_damage_block(
            summary["damage_done"], guild, classes_map, records, tier_name
        )
        if damage_block:
            pre_loot_blocks.append(self._text_block(damage_block))
        deaths_block, deaths_record_update = self._build_deaths_block(
            summary["deaths"], guild, classes_map, records, tier_name
        )
        if deaths_block:
            pre_loot_blocks.append(self._text_block(deaths_block))
        healing_block, healing_record_update = self._build_healing_block(
            summary["healing_done"], guild, classes_map, records, tier_name
        )
        if healing_block:
            pre_loot_blocks.append(self._text_block(healing_block))
        overheal_block, overheal_record_update = self._build_overheal_block(
            summary.get("overheal_pct", {}), healer_names, guild, classes_map, records, tier_name
        )
        if overheal_block:
            pre_loot_blocks.append(self._text_block(overheal_block))

        activity_block, activity_record_update = self._build_ranked_block(
            "Top Activity %", "⚡", summary.get("activity", {}), guild, classes_map,
            lambda v: f"{v:.1f}%", records=records, tier_name=tier_name, stat_key="activity_pct",
            direction="high", delta_fmt=lambda v: f"{v:.2f}%",
        )
        if activity_block:
            pre_loot_blocks.append(self._text_block(activity_block))
        potions_block, potions_record_update = await self._build_potions_block(
            summary.get("potion_casts", {}), guild, classes_map, existing_by_name, records, tier_name
        )
        if potions_block:
            pre_loot_blocks.append(self._text_block(potions_block))

        interrupts_icon = await self._resolve_spell_icon(config.TOP_INTERRUPTERS_ICON_SPELL_ID, existing_by_name) or "⛔"
        interrupts_block, interrupts_record_update = self._build_ranked_block(
            "Top Interrupters", interrupts_icon, summary.get("interrupts", {}), guild, classes_map,
            lambda v: f"{int(v)} interrupt{'s' if int(v) != 1 else ''}",
            records=records, tier_name=tier_name, stat_key="interrupts", direction="high",
            delta_fmt=lambda v: f"{int(round(v))}",
        )
        if interrupts_block:
            pre_loot_blocks.append(self._text_block(interrupts_block))

        dispels_icon = await self._resolve_spell_icon(config.TOP_DISPELLERS_ICON_SPELL_ID, existing_by_name) or "🧹"
        dispels_block, dispels_record_update = self._build_ranked_block(
            "Top Dispellers", dispels_icon, summary.get("dispels", {}), guild, classes_map,
            lambda v: f"{int(v)} dispel{'s' if int(v) != 1 else ''}",
            records=records, tier_name=tier_name, stat_key="dispels", direction="high",
            delta_fmt=lambda v: f"{int(round(v))}",
        )
        if dispels_block:
            pre_loot_blocks.append(self._text_block(dispels_block))

        uptime_lines, uptime_updates = await self._build_uptime_lines(
            aura_uptime, records, tier_name, guild, classes_map, existing_by_name
        )
        if uptime_lines:
            pre_loot_blocks.append(self._text_block("**Buff/Debuff Uptime**\n" + "\n".join(uptime_lines)))

        tier_stat_updates = {
            k: v for k, v in {
                "damage_done": damage_record_update, "most_deaths": deaths_record_update,
                "healing_done": healing_record_update, "overheal_pct_lowest": overheal_record_update,
                "activity_pct": activity_record_update, "potions_used": potions_record_update,
                "interrupts": interrupts_record_update, "dispels": dispels_record_update,
            }.items() if v is not None
        }

        loot_lines = await self._build_loot_lines(resolved_loot, guild, classes_map, existing_by_name)
        loot_blocks = self._build_loot_blocks(loot_lines)

        banner_source, banner_file = self._load_banner(tier_data["name"])
        page_views = self._render_pages(pre_loot_blocks, loot_blocks, header_ctx, note, media_link, banner_source)

        applied_tags = self._resolve_applied_tags(forum_channel, tier_data["name"], clear_status, raid_type)

        thread_name = thread_name_override or f"{tier_data['name']} — {report_date}"
        try:
            create_kwargs = {"name": thread_name, "view": page_views[0], "applied_tags": applied_tags}
            if banner_file:
                create_kwargs["file"] = banner_file
            thread_result = await forum_channel.create_thread(**create_kwargs)
            thread = thread_result.thread
            posted_messages = [thread_result.message]
            for view in page_views[1:]:
                posted_messages.append(await thread.send(view=view))
        except Exception:
            log.exception("Failed to post raid summary to forum")
            return {
                "ok": False,
                "error": ("Something went wrong posting the summary - check the bot's permissions on the "
                          "raid-summary forum channel (Send Messages, Create Posts, Embed Links)."),
            }

        # Only commit kill/clear-time/parse/uptime/tier-stat records once the post actually succeeded.
        if (newly_killed_ids or new_fastest_kills or clear_time_updates or parse_updates
                or uptime_updates or tier_stat_updates):
            for encounter_id in newly_killed_ids:
                records["encounters"].setdefault(str(encounter_id), {})["first_seen_report"] = report_code
                records["encounters"][str(encounter_id)]["first_seen_date"] = report_date
            for encounter_id, duration_ms in new_fastest_kills.items():
                entry = records["encounters"].setdefault(str(encounter_id), {})
                entry["fastest_ms"] = duration_ms
                entry["fastest_report"] = report_code
                entry["fastest_date"] = report_date
            for instance_name, new_fastest_ms in clear_time_updates.items():
                records["clears"][instance_name] = {
                    "fastest_ms": new_fastest_ms, "fastest_report": report_code, "fastest_date": report_date,
                }
            for encounter_id, name_map in parse_updates.items():
                boss_bests = records["parses"].setdefault(str(encounter_id), {})
                for name, pct in name_map.items():
                    boss_bests[name] = {"best_percent": pct, "report_code": report_code, "date": report_date}
            tier_buffs = records["buffs"].setdefault(tier_name, {})
            for ability_name, new_best_pct in uptime_updates.items():
                tier_buffs[ability_name] = {
                    "best_uptime_pct": new_best_pct, "report_code": report_code, "date": report_date,
                }
            tier_stats_bucket = records["tier_stats"].setdefault(tier_name, {})
            for stat_key, update in tier_stat_updates.items():
                tier_stats_bucket[stat_key] = {**update, "report_code": report_code, "date": report_date}
            self._save_records(records)

        # Persisted so the ✏️ Edit / 🎁 Add Loot buttons can rebuild the
        # summary later without re-touching WCL/Wowhead or the records
        # above - see the module docstring.
        last_message = posted_messages[-1]
        self.store.set(
            last_message.id,
            thread_id=thread.id,
            report_code=report_code,
            page_message_ids=[m.id for m in posted_messages],
            pre_loot_blocks=pre_loot_blocks,
            loot_blocks=loot_blocks,
            header_ctx=header_ctx,
            note=note,
            media_link=media_link,
            banner_url=banner_source,
        )

        self._record_tier_report(tier_name, report_code, raid_type)

        return {"ok": True, "thread": thread}

    # --- bulk (one-time) import of historic reports --------------------------

    async def _wait_for_rate_limit_budget(self, min_fraction_remaining: float = 0.15):
        """
        Checks WCL's own live rate-limit counter (wcl_client.get_rate_limit_status)
        before an expensive per-report fetch and sleeps until the hourly
        window resets if less than min_fraction_remaining of the budget is
        left - used by the bulk-import job so a long overnight run paces
        itself against whatever the account's ACTUAL plan allows instead of
        a guessed fixed delay (WCL v2's API is points-based, cost varies by
        query complexity, so a fixed delay would either be overly
        conservative on a generous plan or still run out on a smaller one).
        If the check itself fails (best-effort, see that method), falls
        back to a short fixed pause rather than blocking forever or
        barrelling ahead blind.
        """
        status = await self.bot.wcl.get_rate_limit_status()
        if status is None:
            await asyncio.sleep(5)
            return
        limit = status.get("limit_per_hour")
        spent = status.get("points_spent")
        if not limit or spent is None:
            return
        remaining_fraction = (limit - spent) / limit
        if remaining_fraction < min_fraction_remaining:
            wait_seconds = (status.get("points_reset_in") or 3600) + 5
            log.info(
                "Bulk import: WCL rate-limit budget low (%.0f%% left) - sleeping %ds for reset",
                remaining_fraction * 100, wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

    async def _run_bulk_import(self, channel, forum_channel: discord.ForumChannel, tier_data: dict,
                                report_codes: list, raid_type: str):
        """
        Background task (deliberately NOT awaited by the slash command
        handler, and never touches the triggering interaction) that posts
        one raid-summary thread per report code, in order, titled
        "<tier> - Week <N> - <date>" (oldest = week 1, per raidsummary_bulk).
        Discord's interaction/webhook token expires 15 minutes after the
        command was invoked - far too short for a run explicitly meant to
        go overnight - so progress/failures are posted as plain messages in
        `channel` instead of interaction followups, and Full Clear/Progress
        is auto-derived from this tier's boss-kill data (every configured
        boss killed = Full Clear) since there's no moderator present to
        pick from the dropdown for a batch of historic reports. Paced
        against WCL's live rate-limit counter (_wait_for_rate_limit_budget)
        plus a small fixed pause between reports either way, to stay easy
        on Discord's own forum-thread-creation rate limit too.
        """
        total = len(report_codes)
        await channel.send(f"▶️ Bulk import started: {total} report(s) for **{tier_data['name']}**.")
        posted = 0
        for i, raw in enumerate(report_codes, start=1):
            report_code = _extract_report_code(raw)
            await self._wait_for_rate_limit_budget()

            try:
                summary = await self.bot.wcl.get_report_summary(report_code)
            except Exception:
                log.exception("Bulk import: failed to fetch report %s", report_code)
                await channel.send(f"❌ [{i}/{total}] `{report_code}` — couldn't fetch this report, skipped.")
                continue
            if not summary.get("fights"):
                await channel.send(f"❌ [{i}/{total}] `{report_code}` — no fights, skipped.")
                continue

            report_date = self._report_timing(summary)["date"]
            thread_name = f"{tier_data['name']} - Week {i} - {report_date}"

            fights_by_encounter = self._group_fights_by_encounter(summary["fights"])
            killed_count, _, _ = self._tier_stats(tier_data, fights_by_encounter)
            clear_status = "full_clear" if killed_count == len(tier_data["bosses"]) else "progress"

            try:
                result = await self._assemble_and_post_summary(
                    channel.guild, forum_channel, tier_data, report_code, clear_status, raid_type,
                    [], None, None, thread_name_override=thread_name,
                )
            except Exception:
                log.exception("Bulk import: unexpected error on report %s", report_code)
                await channel.send(f"❌ [{i}/{total}] `{report_code}` — unexpected error, check the bot's logs.")
                continue

            if not result["ok"]:
                await channel.send(f"❌ [{i}/{total}] `{report_code}` — {result['error']}")
                continue

            posted += 1
            await channel.send(f"✅ [{i}/{total}] {thread_name} — {result['thread'].mention}")
            await asyncio.sleep(3)  # stay easy on Discord's own forum-thread-creation rate limit

        await channel.send(f"🏁 Bulk import finished: {posted}/{total} posted.")

    @app_commands.command(
        name="raidsummary-bulk",
        description="One-time bulk import: post a raid summary per WCL report, oldest first (moderator only)",
    )
    @app_commands.describe(
        tier="Which tier these reports belong to",
        raid_type="Main raid or alt/fun raid",
        reports=f"Ordered WCL report links/codes, OLDEST FIRST, separated by spaces/commas/newlines (max {MAX_BULK_REPORTS})",
    )
    @app_commands.choices(
        tier=[
            app_commands.Choice(name=config.CURRENT_TIER["name"], value=config.CURRENT_TIER["name"]),
            app_commands.Choice(name=config.PREVIOUS_TIER["name"], value=config.PREVIOUS_TIER["name"]),
        ],
        raid_type=[
            app_commands.Choice(name="Main Raid", value="main"),
            app_commands.Choice(name="Alt Raid", value="alt"),
        ],
    )
    async def raidsummary_bulk(self, interaction: discord.Interaction, tier: str, raid_type: str, reports: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can bulk-import raid summaries.", ephemeral=True)
            return

        forum_channel = self.bot.get_channel(self.forum_channel_id)
        if forum_channel is None or not isinstance(forum_channel, discord.ForumChannel):
            await interaction.response.send_message(
                "RAID_SUMMARY_FORUM_CHANNEL_ID isn't set to a real forum channel the bot can see.", ephemeral=True
            )
            return

        report_codes = [r for r in re.split(r"[,\s]+", reports.strip()) if r]
        if not report_codes:
            await interaction.response.send_message("No report links/codes found in that list.", ephemeral=True)
            return
        if len(report_codes) > MAX_BULK_REPORTS:
            await interaction.response.send_message(
                f"That's {len(report_codes)} reports - please do at most {MAX_BULK_REPORTS} per run.", ephemeral=True
            )
            return

        tier_data = self._resolve_tier(tier)
        await interaction.response.send_message(
            f"Starting bulk import of {len(report_codes)} report(s) for **{tier_data['name']}** - progress will "
            f"be posted in this channel. This paces itself against WarcraftLogs' own API rate limit, so it can "
            f"take a while on a big batch - safe to leave running.",
            ephemeral=True,
        )
        task = asyncio.create_task(
            self._run_bulk_import(interaction.channel, forum_channel, tier_data, report_codes, raid_type)
        )
        # A task with no strong reference can be garbage-collected mid-run -
        # this list keeps one until the task finishes (see done_callback).
        self._bulk_tasks.append(task)
        task.add_done_callback(lambda t: self._bulk_tasks.remove(t) if t in self._bulk_tasks else None)

    async def _run_cache_refresh(self, channel, tier_data: dict):
        """
        Background task (same "long-running, can't rely on the interaction
        token" reasoning as _run_bulk_import) that forces EVERY report on
        record for this tier - regardless of raid_type, since an alt/fun
        raid's cached fights can be just as contaminated by off-tier
        content as a main raid's - to be re-fetched from WCL from scratch
        via wcl_client.invalidate_report(). Used after a fight-inclusion
        rule changes (e.g. config.EXCLUDED_ENCOUNTER_IDS gaining an entry)
        so already-imported reports pick up the correction without a full
        re-import.

        Deliberately does NOT touch any already-posted #raid-summary
        thread message - those are frozen at post time by design (see
        raid_summary.py's module docstring on why records aren't
        recomputed retroactively). Only the underlying WCL cache is
        corrected - which is exactly what /tier-recap reads fresh from on
        every draft/regenerate, so this is sufficient to fix the tier
        retrospective's numbers (unique roster, fastest clear/raid-night,
        every summed stat) without editing anything already public.
        """
        codes = self.get_tier_reports(tier_data["name"], raid_type=None)
        total = len(codes)
        await channel.send(f"▶️ Cache refresh started: {total} report(s) for **{tier_data['name']}**.")
        refreshed = 0
        for i, code in enumerate(codes, start=1):
            await self._wait_for_rate_limit_budget()
            self.bot.wcl.invalidate_report(code)
            try:
                summary = await self.bot.wcl.get_report_summary(code)
            except Exception:
                log.exception("Cache refresh: failed to refetch report %s", code)
                await channel.send(f"❌ [{i}/{total}] `{code}` — couldn't refetch this report, skipped.")
                continue
            if not summary.get("fights"):
                await channel.send(f"⚠️ [{i}/{total}] `{code}` — no fights left after refresh.")
            refreshed += 1
            await asyncio.sleep(1)  # stay easy on WCL even between budget checks

        await channel.send(
            f"🏁 Cache refresh finished: {refreshed}/{total} report(s) re-fetched. Run `/tier-recap` again "
            f"(🔄 Regenerate if a draft already exists) to see corrected numbers - already-posted "
            f"#raid-summary threads are NOT retroactively edited."
        )

    @app_commands.command(
        name="raidsummary-refresh-cache",
        description="Re-fetch already-imported reports from WCL to pick up a fight-filtering change (moderator only)",
    )
    @app_commands.describe(tier="Which tier's already-imported reports to refresh")
    @app_commands.choices(
        tier=[
            app_commands.Choice(name=config.CURRENT_TIER["name"], value=config.CURRENT_TIER["name"]),
            app_commands.Choice(name=config.PREVIOUS_TIER["name"], value=config.PREVIOUS_TIER["name"]),
        ],
    )
    async def raidsummary_refresh_cache(self, interaction: discord.Interaction, tier: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can refresh the report cache.", ephemeral=True)
            return

        tier_data = self._resolve_tier(tier)
        codes = self.get_tier_reports(tier_data["name"], raid_type=None)
        if not codes:
            await interaction.response.send_message(f"No reports on record for **{tier_data['name']}**.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Starting cache refresh of {len(codes)} report(s) for **{tier_data['name']}** - progress will be "
            f"posted in this channel. This does NOT edit any already-posted #raid-summary thread - it only "
            f"re-fetches and corrects the underlying WCL cache that /tier-recap reads from. Safe to leave running.",
            ephemeral=True,
        )
        task = asyncio.create_task(self._run_cache_refresh(interaction.channel, tier_data))
        self._bulk_tasks.append(task)
        task.add_done_callback(lambda t: self._bulk_tasks.remove(t) if t in self._bulk_tasks else None)

    @app_commands.command(
        name="raidsummary-merge-weeks",
        description="Fold 2+ already-imported reports into ONE raid week for /tier-recap (moderator only)",
    )
    @app_commands.describe(
        tier="Which tier these reports belong to",
        reports="The WCL report links/codes to merge into one week, separated by spaces/commas/newlines",
    )
    @app_commands.choices(
        tier=[
            app_commands.Choice(name=config.CURRENT_TIER["name"], value=config.CURRENT_TIER["name"]),
            app_commands.Choice(name=config.PREVIOUS_TIER["name"], value=config.PREVIOUS_TIER["name"]),
        ],
    )
    async def raidsummary_merge_weeks(self, interaction: discord.Interaction, tier: str, reports: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can merge raid weeks.", ephemeral=True)
            return

        codes = [_extract_report_code(r) for r in re.split(r"[,\s]+", reports.strip()) if r]
        if len(codes) < 2:
            await interaction.response.send_message(
                "Give at least 2 report links/codes to merge into one week.", ephemeral=True
            )
            return

        tier_data = self._resolve_tier(tier)
        on_record = set(self.get_tier_reports(tier_data["name"], raid_type=None))
        missing = [c for c in codes if c not in on_record]
        if missing:
            await interaction.response.send_message(
                f"These aren't on record for **{tier_data['name']}** (post/import them first): "
                + ", ".join(f"`{c}`" for c in missing),
                ephemeral=True,
            )
            return

        self.merge_tier_reports(tier_data["name"], codes)
        await interaction.response.send_message(
            f"✅ Merged {len(codes)} reports into one raid week for **{tier_data['name']}**: "
            + ", ".join(f"`{c}`" for c in codes)
            + "\nRun `/tier-recap` again (🔄 Regenerate if a draft already exists) to see the corrected week "
              "numbering and combined raid-night duration.",
            ephemeral=True,
        )

    # --- editing / adding loot later ----------------------------------------

    async def _reconcile_pages(self, interaction: discord.Interaction, last_message_id: int,
                                record: dict, page_views: list) -> bool:
        """
        Shared by _apply_edit (note/media) and _on_add_loot_click (loot) -
        edits existing thread messages page-by-page in place, sends new
        ones if the update made the summary longer, deletes leftovers if
        shorter, and re-persists the record under the (possibly new) last
        message's ID. Returns True on success (caller sends its own
        follow-up on success; this sends its own on failure).
        """
        try:
            thread = self.bot.get_channel(record["thread_id"]) or await self.bot.fetch_channel(record["thread_id"])
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send(
                "Couldn't find the original thread - it may have been deleted.", ephemeral=True
            )
            return False

        old_ids = record["page_message_ids"]
        new_ids = []
        try:
            for i, view in enumerate(page_views):
                if i < len(old_ids):
                    try:
                        message = await thread.fetch_message(old_ids[i])
                        await message.edit(view=view)
                        new_ids.append(message.id)
                        continue
                    except (discord.NotFound, discord.Forbidden):
                        pass
                new_ids.append((await thread.send(view=view)).id)

            for stale_id in old_ids[len(page_views):]:
                try:
                    stale_message = await thread.fetch_message(stale_id)
                    await stale_message.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        except Exception:
            log.exception("Failed to reconcile raid summary pages")
            await interaction.followup.send("Something went wrong updating the summary.", ephemeral=True)
            return False

        record["page_message_ids"] = new_ids
        if new_ids[-1] != last_message_id:
            self.store.delete(last_message_id)
        self.store.set(new_ids[-1], **record)
        return True

    async def _on_edit_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can edit raid summaries.", ephemeral=True)
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this summary's saved data - it may predate the edit feature.", ephemeral=True
            )
            return
        modal = RaidSummaryEditModal(self, interaction.message.id, record.get("note"), record.get("media_link"))
        await interaction.response.send_modal(modal)

    async def _apply_edit(self, interaction: discord.Interaction, last_message_id: int, note, media_link):
        record = self.store.get(last_message_id)
        if record is None:
            await interaction.followup.send("Couldn't find this summary's saved data.", ephemeral=True)
            return

        record["note"] = note
        record["media_link"] = media_link
        page_views = self._render_pages(
            record["pre_loot_blocks"], record["loot_blocks"], record["header_ctx"],
            note, media_link, record.get("banner_url"),
        )
        if await self._reconcile_pages(interaction, last_message_id, record, page_views):
            await interaction.followup.send("Summary updated.", ephemeral=True)

    async def _on_add_loot_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can add loot.", ephemeral=True)
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this summary's saved data - it may predate the Add Loot feature.", ephemeral=True
            )
            return

        last_message_id = interaction.message.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        await interaction.response.send_message(
            f"Reply in this thread with the Gargul loot export as a **file attachment** "
            f"(.csv/.txt - no size limit, unlike pasting into `/raidsummary` directly) within "
            f"{ADD_LOOT_WAIT_SECONDS // 60} minutes.",
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            return m.channel.id == channel_id and m.author.id == user_id and bool(m.attachments)

        try:
            upload_message = await self.bot.wait_for("message", check=check, timeout=ADD_LOOT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Timed out waiting for the loot export - click 🎁 Add/Update Loot again.", ephemeral=True
            )
            return

        try:
            raw_bytes = await upload_message.attachments[0].read()
            loot_rows = gargul_loot.parse_gargul_export(raw_bytes.decode("utf-8", errors="replace"))
        except gargul_loot.GargulParseError as e:
            await interaction.followup.send(f"Couldn't read that loot export: {e}", ephemeral=True)
            return
        except Exception:
            log.exception("Failed to read/parse loot export from the Add Loot flow")
            await interaction.followup.send("Couldn't read that loot export file.", ephemeral=True)
            return

        resolved_loot = await self._resolve_loot(loot_rows)

        # Re-fetch composition for class icons on the winner names - cheap,
        # since get_report_role_composition caches per report_code and this
        # report was already summarized once at post time.
        classes_map = {}
        report_code = record.get("report_code")
        if report_code:
            try:
                composition = await self.bot.wcl.get_report_role_composition(report_code)
                classes_map = (composition or {}).get("classes", {})
            except Exception:
                log.warning("Role composition lookup failed while adding loot for %s", report_code, exc_info=True)

        existing_by_name = await self._fetch_existing_app_emojis()
        loot_lines = await self._build_loot_lines(resolved_loot, interaction.guild, classes_map, existing_by_name)

        record["loot_blocks"] = self._build_loot_blocks(loot_lines)
        record["header_ctx"]["loot_count"] = len(resolved_loot)
        record["header_ctx"]["loot_pending"] = False

        page_views = self._render_pages(
            record["pre_loot_blocks"], record["loot_blocks"], record["header_ctx"],
            record.get("note"), record.get("media_link"), record.get("banner_url"),
        )
        ok = await self._reconcile_pages(interaction, last_message_id, record, page_views)

        try:
            await upload_message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        if ok:
            await interaction.followup.send(f"Added {len(resolved_loot)} loot item(s) to the summary.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidSummaryCog(bot))
