"""
Raid summary feature - posts a presentable per-raid recap (banner, links to
the full log + Wipefest analysis, a compact loot list with real item icons/
Wowhead links, boss-by-boss pulls with kill time + wipe breakdown + fastest-
kill tracking, elite parses, personal bests, top damage, roster composition,
guild rank, a death leaderboard) as a new thread in a Discord forum channel,
so raiders have somewhere to discuss each raid night.

Design, per discussion:
  - A moderator runs /raidsummary once per raid, giving it: which tier was
    raided, the WCL report link, whether it was a full clear or still
    progress, whether it's a main or alt/fun raid, and optionally the
    Gargul loot export (as a file attachment - can be added later, see
    below), a note, and a media link (YouTube/Twitch clip or an image) to
    feature.
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

log = logging.getLogger("wow-apply-bot.raidsummary")

REPORT_LINK_RE = re.compile(r"(?:reports/|^)([A-Za-z0-9]{8,20})(?:[/#].*)?$")

RECORDS_KEY = "raid_summary_records"
EDIT_BUTTON_CUSTOM_ID = "raidsummary_edit_btn"
ADD_LOOT_BUTTON_CUSTOM_ID = "raidsummary_addloot_btn"
ADD_LOOT_WAIT_SECONDS = 300

QUALITY_EMOJI = {0: "⬜", 1: "⬜", 2: "🟩", 3: "🟦", 4: "🟪", 5: "🟧"}
DEFAULT_QUALITY_EMOJI = "⬜"

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
        label="Media link - YouTube/Twitch/image URL (optional)", required=False, max_length=300,
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


class RaidSummaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.forum_channel_id = int(os.environ["RAID_SUMMARY_FORUM_CHANNEL_ID"])
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.server_slug = os.environ["SERVER_SLUG"]
        self.server_region = os.environ.get("SERVER_REGION", "us")
        self.guild_name = os.environ.get("GUILD_NAME")  # optional - enables the guild-rank section

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
        record = self.bot.store.get(RECORDS_KEY)
        if record is None:
            return {"encounters": {}, "clears": {}, "parses": {}}
        return {
            "encounters": record.get("encounters", {}),
            "clears": record.get("clears", {}),
            "parses": record.get("parses", {}),
        }

    def _save_records(self, records: dict):
        self.bot.store.set(
            RECORDS_KEY, encounters=records["encounters"], clears=records["clears"], parses=records["parses"]
        )

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

    async def _build_loot_lines(self, resolved_loot: list, guild: discord.Guild, classes_map: dict) -> list:
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

        try:
            existing_by_name = {e.name: e for e in await self.bot.fetch_application_emojis()}
        except Exception:
            log.warning("Couldn't fetch application emojis for loot icons", exc_info=True)
            existing_by_name = {}

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

    def _build_boss_lines(self, tier: dict, fights_by_encounter: dict, records: dict) -> tuple:
        """
        Returns (lines, newly_killed_ids, new_fastest_kills) where
        new_fastest_kills is {encounter_id: duration_ms} for every boss this
        raid that either had no prior time on record, or beat/tied it - the
        caller persists these after a successful post. Every non-kill
        (wipe) fight in a boss's group gets its own indented line below the
        boss's summary line, in pull order, showing the boss's remaining
        health % at that wipe.
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

                lines.append(
                    f"✅ **{boss_name}** — killed in {pulls} pull{'s' if pulls != 1 else ''} "
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
                lines.append(f" ↳ Wipe {wipe_number}: {pct_text}")

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

    def _build_deaths_block(self, deaths: dict, guild: discord.Guild, classes_map: dict) -> str:
        if not deaths:
            return ""
        total = sum(deaths.values())
        top = sorted(deaths.items(), key=lambda kv: -kv[1])[:5]
        lines = [
            f"💀 {self._name_icon(guild, classes_map.get(name))}**{name}** — {count} death{'s' if count != 1 else ''}"
            for name, count in top
        ]
        return f"**Death leaderboard** — {total} total death{'s' if total != 1 else ''}\n" + "\n".join(lines)

    def _build_damage_block(self, damage_done: dict, guild: discord.Guild, classes_map: dict) -> str:
        """Top 5 by total damage across the whole report (bosses AND
        trash) - matches WCL's own "Overall" damage-done ranking view,
        including each entry's exact share of the raid's total damage."""
        if not damage_done:
            return ""
        total_damage = sum(damage_done.values()) or 1
        top = sorted(damage_done.items(), key=lambda kv: -kv[1])[:5]
        lines = [
            f"⚔️ {self._name_icon(guild, classes_map.get(name))}**{name}** — "
            f"{amount:,} damage ({amount / total_damage * 100:.2f}%)"
            for name, amount in top
        ]
        return "**Top Overall Damage (bosses + trash)**\n" + "\n".join(lines)

    def _build_parses_block(self, parses: list, fights: list, damage_done: dict, healing_done: dict,
                             guild: discord.Guild, classes_map: dict) -> str:
        """
        "Raid MVP's" - three separate lines: highest DPS parse (rank
        percentile, DPS-role parses only - healers/tanks have their own
        separate WCL metrics, not comparable on the same percentile scale),
        highest overall damage done, and highest overall healing done.
        Followed by the elite (>= config.PARSE_HIGHLIGHT_THRESHOLD) parse
        callouts, any role.
        """
        fight_names = {f["id"]: f["name"] for f in fights}
        with_pct = [p for p in parses if p.get("rank_percent") is not None]

        mvp_lines = []
        dps_parses = [p for p in with_pct if p.get("role") == "dps"]
        if dps_parses:
            top = max(dps_parses, key=lambda p: p["rank_percent"])
            mvp_lines.append(
                f"🏆 {self._name_icon(guild, top.get('class'))}**{top['name']}** — highest DPS parse "
                f"({top['rank_percent']:.1f}% on {_boss_name(top, fight_names)})"
            )
        if damage_done:
            name, amount = max(damage_done.items(), key=lambda kv: kv[1])
            mvp_lines.append(
                f"🏆 {self._name_icon(guild, classes_map.get(name))}**{name}** — "
                f"highest damage done ({amount:,})"
            )
        if healing_done:
            name, amount = max(healing_done.items(), key=lambda kv: kv[1])
            mvp_lines.append(
                f"🏆 {self._name_icon(guild, classes_map.get(name))}**{name}** — "
                f"highest healing done ({amount:,})"
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
            for p in elite[:20]:
                spec_class = " ".join(x for x in (p.get("spec"), p.get("class")) if x)
                suffix = f" ({spec_class})" if spec_class else ""
                lines.append(
                    f"🌟 {self._name_icon(guild, p.get('class'))}**{p['name']}** — {p['rank_percent']:.1f}% on "
                    f"{_boss_name(p, fight_names)}{suffix}"
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
    @app_commands.describe(
        tier="Which raid tier was raided",
        report="WCL report link (or bare report code)",
        clear_status="Full clear or still progressing?",
        raid_type="Main raid or an alt/fun raid?",
        loot_export="Gargul loot export (.csv/.txt) - optional, add later with the Add Loot button if not ready yet",
        media_link="Optional: a YouTube/Twitch clip or image URL to feature",
        note="Optional short note/highlight for the top of the summary",
    )
    @app_commands.choices(
        tier=[
            app_commands.Choice(name=config.CURRENT_TIER["name"], value=config.CURRENT_TIER["name"]),
            app_commands.Choice(name=config.PREVIOUS_TIER["name"], value=config.PREVIOUS_TIER["name"]),
        ],
        clear_status=[
            app_commands.Choice(name="Full Clear", value="full_clear"),
            app_commands.Choice(name="Progress", value="progress"),
        ],
        raid_type=[
            app_commands.Choice(name="Main Raid", value="main"),
            app_commands.Choice(name="Alt Raid", value="alt"),
        ],
    )
    async def raidsummary(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        report: str,
        clear_status: app_commands.Choice[str],
        raid_type: app_commands.Choice[str],
        loot_export: discord.Attachment = None,
        media_link: str = None,
        note: str = None,
    ):
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

        await interaction.response.defer(ephemeral=True, thinking=True)

        # --- parse the loot export, if given now ---
        loot_rows = []
        if loot_export is not None:
            try:
                raw_bytes = await loot_export.read()
                loot_rows = gargul_loot.parse_gargul_export(raw_bytes.decode("utf-8", errors="replace"))
            except gargul_loot.GargulParseError as e:
                await interaction.followup.send(f"Couldn't read the loot export: {e}", ephemeral=True)
                return
            except Exception:
                log.exception("Failed to read/parse loot_export attachment")
                await interaction.followup.send("Couldn't read the loot export file.", ephemeral=True)
                return

        # --- fetch the WCL report (single cached fetch, shared with attendance) ---
        report_code = _extract_report_code(report)
        try:
            summary = await self.bot.wcl.get_report_summary(report_code)
        except Exception:
            log.exception("Failed to fetch WCL report summary for %s", report_code)
            await interaction.followup.send(
                f"Couldn't fetch WCL report `{report_code}` - check the link/code and try again.",
                ephemeral=True,
            )
            return
        if not summary.get("fights"):
            await interaction.followup.send(
                f"WCL report `{report_code}` has no fights - double check the link.", ephemeral=True
            )
            return

        # --- resolve loot item data ---
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

        tier_data = self._resolve_tier(tier.value)
        fights_by_encounter = self._group_fights_by_encounter(summary["fights"])
        records = self._get_records()
        boss_lines, newly_killed_ids, new_fastest_kills = self._build_boss_lines(
            tier_data, fights_by_encounter, records
        )
        killed_count, attempted_count, total_pulls = self._tier_stats(tier_data, fights_by_encounter)
        clear_label = "Full Clear!" if clear_status.value == "full_clear" else "Progress Raid"

        # "First pull" is the first REAL boss pull, not the report's own
        # start (which can include several minutes of trash/travel before
        # the first pull) - Fight.start_time is relative to the report's
        # absolute start, so it has to be added back on to get a real clock
        # time. Total duration is measured from that same first-pull anchor
        # to the raid's end, so the three numbers on this line are always
        # mutually consistent.
        report_date = "?"
        duration_line = ""
        encounter_fights = [f for f in summary["fights"] if f.get("encounter_id") is not None]
        first_pull_relative_ms = (
            min((f["start_time"] for f in encounter_fights if f["start_time"] is not None), default=None)
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

        clear_time_lines, clear_time_updates = self._build_clear_time_lines(tier_data, fights_by_encounter, records)

        header_ctx = {
            "clear_label": clear_label, "tier_name": tier_data["name"], "report_date": report_date,
            "killed_count": killed_count, "attempted_count": attempted_count, "total_pulls": total_pulls,
            "loot_count": len(resolved_loot), "loot_pending": loot_export is None,
            "duration_line": duration_line, "clear_time_lines": clear_time_lines,
        }

        pre_loot_blocks = [self._text_block(self._build_links_block(report_code))]

        comp_block = self._build_comp_block(composition)
        if comp_block:
            pre_loot_blocks.append(self._text_block(comp_block))
        if boss_lines:
            pre_loot_blocks.append(self._text_block("**Boss-by-boss**\n" + "\n".join(boss_lines)))
        parses_block = self._build_parses_block(
            summary["parses"], summary["fights"], summary["damage_done"], summary["healing_done"],
            interaction.guild, classes_map,
        )
        if parses_block:
            pre_loot_blocks.append(self._text_block(parses_block))
        personal_bests_block, parse_updates = self._build_personal_bests_block(
            summary["parses"], summary["fights"], records, interaction.guild
        )
        if personal_bests_block:
            pre_loot_blocks.append(self._text_block(personal_bests_block))
        guild_rank_block = await self._build_guild_rank_block(tier_data, fights_by_encounter)
        if guild_rank_block:
            pre_loot_blocks.append(self._text_block(guild_rank_block))
        damage_block = self._build_damage_block(summary["damage_done"], interaction.guild, classes_map)
        if damage_block:
            pre_loot_blocks.append(self._text_block(damage_block))
        deaths_block = self._build_deaths_block(summary["deaths"], interaction.guild, classes_map)
        if deaths_block:
            pre_loot_blocks.append(self._text_block(deaths_block))

        loot_lines = await self._build_loot_lines(resolved_loot, interaction.guild, classes_map)
        loot_blocks = self._build_loot_blocks(loot_lines)

        banner_source, banner_file = self._load_banner(tier_data["name"])
        page_views = self._render_pages(pre_loot_blocks, loot_blocks, header_ctx, note, media_link, banner_source)

        applied_tags = self._resolve_applied_tags(forum_channel, tier_data["name"], clear_status.value, raid_type.value)

        thread_name = f"{tier_data['name']} — {report_date}"
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
            await interaction.followup.send(
                "Something went wrong posting the summary - check the bot's permissions on the "
                "raid-summary forum channel (Send Messages, Create Posts, Embed Links).",
                ephemeral=True,
            )
            return

        # Only commit kill/clear-time/parse records once the post actually succeeded.
        if newly_killed_ids or new_fastest_kills or clear_time_updates or parse_updates:
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
            self._save_records(records)

        # Persisted so the ✏️ Edit / 🎁 Add Loot buttons can rebuild the
        # summary later without re-touching WCL/Wowhead or the records
        # above - see the module docstring.
        last_message = posted_messages[-1]
        self.bot.store.set(
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

        await interaction.followup.send(f"Posted: {thread.mention}", ephemeral=True)

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
            self.bot.store.delete(last_message_id)
        self.bot.store.set(new_ids[-1], **record)
        return True

    async def _on_edit_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can edit raid summaries.", ephemeral=True)
            return
        record = self.bot.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this summary's saved data - it may predate the edit feature.", ephemeral=True
            )
            return
        modal = RaidSummaryEditModal(self, interaction.message.id, record.get("note"), record.get("media_link"))
        await interaction.response.send_modal(modal)

    async def _apply_edit(self, interaction: discord.Interaction, last_message_id: int, note, media_link):
        record = self.bot.store.get(last_message_id)
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
        record = self.bot.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this summary's saved data - it may predate the Add Loot feature.", ephemeral=True
            )
            return

        last_message_id = interaction.message.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        await interaction.response.send_message(
            f"Reply in this thread with the Gargul loot export (.csv/.txt attachment) within "
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

        loot_lines = await self._build_loot_lines(resolved_loot, interaction.guild, classes_map)

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
