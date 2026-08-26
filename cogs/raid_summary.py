"""
Raid summary feature - posts a presentable per-raid recap (banner, loot with
Wowhead links/icons, boss-by-boss pulls with kill time + fastest-kill
tracking, elite parses, guild rank, "fun stats", and a link to the full log)
as a new thread in a Discord forum channel, so raiders have somewhere to
discuss each raid night.

Design, per discussion:
  - A moderator runs /raidsummary once per raid, giving it: which tier was
    raided, the WCL report link, the Gargul loot export (as a file
    attachment - Gargul's export has no size limit worth worrying about,
    unlike a modal's 4000-char text field), whether it was a full clear or
    still progress, and optionally a note + a media link (YouTube/Twitch
    clip or an image) to feature.
  - All WCL data for the report (fights/pulls, parse rankings, deaths,
    roster/comp) comes from ONE cached fetch - wcl_client.WarcraftLogsClient.
    get_report_summary() - shared with cogs/attendance.py's attendance
    check. If a log's already been parsed for attendance (or vice versa),
    generating its raid summary costs zero extra WCL requests for anything
    that fetch already covers.
  - Gargul's export gives itemID + winner only (no item name/icon/boss) -
    see gargul_loot.py. Item name/icon/Wowhead link are resolved via
    wowhead.py (permanently cached locally, itemID -> name never changes).
    Boss attribution is NOT attempted (no reliable source for it - see
    gargul_loot.py's docstring), so loot is shown as one chronological list
    in award order, not grouped per boss.
  - Kill-time/clear-time records: self._get_records() persists, per
    encounter and per raid zone, the fastest time we've ever recorded (plus
    the report/date it happened) - see RECORDS_KEY. Every posted summary
    compares against that record and shows the delta + a ⚡ badge if it's a
    new (or tied) fastest; a boss/zone with no prior record just gets its
    time recorded silently (nothing to compare against yet). Records are
    only ever updated by a fresh /raidsummary post, never by editing one.
  - Editing: only the note and media link are editable after posting (via
    the persistent ✏️ Edit button on the summary's last message) -
    everything else (boss lines, parses, loot, deaths, guild rank) is
    computed ONCE at post time and frozen. This is deliberate, not a
    shortcut: those sections read from the fastest-kill/first-kill records,
    which get UPDATED at post time - recomputing them on every edit would
    make an already-posted "first kill!"/"fastest!" badge silently change
    (or disappear) later, since by then the record equals itself. Freezing
    them avoids that whole class of bug, and it's cheap since it's exactly
    what needed editing per the ask ("add a clip/screenshot later").
  - Discord's Components V2 caps a single message at ~4000 chars of text
    across all TextDisplay components (and a component-count budget) - loot
    especially can blow past that for a big clear, so the whole summary is
    built as a list of small "blocks" (banner/text/loot-row) which get
    packed into as many separate forum-thread messages as needed - see
    _paginate_blocks(). The first message becomes the thread's OP (with the
    tier + clear-status forum tags applied); the rest are follow-up posts
    in the same thread. Editing re-paginates from the frozen middle blocks +
    fresh tldr/footer text, and reconciles against however many messages
    existed before (edits messages in place, sends new ones if the edit
    made the summary longer, deletes leftovers if it made it shorter).
  - Self-contained like every other cog here - a future feature is a new
    cog file, not changes to this one.
"""

import os
import re
import logging
from collections import Counter
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
import gargul_loot

log = logging.getLogger("wow-apply-bot.raidsummary")

REPORT_LINK_RE = re.compile(r"(?:reports/|^)([A-Za-z0-9]{8,20})(?:[/#].*)?$")

RECORDS_KEY = "raid_summary_records"
EDIT_BUTTON_CUSTOM_ID = "raidsummary_edit_btn"

QUALITY_EMOJI = {0: "⬜", 1: "⬜", 2: "🟩", 3: "🟦", 4: "🟪", 5: "🟧"}
DEFAULT_QUALITY_EMOJI = "⬜"

ROLE_DISPLAY = {"dps": "DPS", "healers": "Healers", "tanks": "Tanks"}

# Conservative budgets per forum-thread message - see the module docstring's
# note on Components V2's ~4000-char/40-component caps. Kept comfortably
# under both since the exact component-counting rules for nested
# Section/Thumbnail accessories aren't documented precisely enough to cut it
# close.
MAX_CHARS_PER_PAGE = 3500
MAX_UNITS_PER_PAGE = 24


def _extract_report_code(link: str) -> str:
    """Accepts a bare report code or a full WCL report URL."""
    link = link.strip().rstrip("/")
    match = REPORT_LINK_RE.search(link)
    return match.group(1) if match else link


def _format_duration(ms) -> str:
    """Compact 'Xh Ym Zs' style - used for the plain raid-duration stat."""
    if not ms or ms < 0:
        return "?"
    total_seconds = int(ms / 1000)
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def _format_clock(ms) -> str:
    """'m:ss' or 'h:mm:ss' - for a single boss kill time, e.g. '4:39'."""
    if not ms or ms < 0:
        return "?"
    total_seconds = int(ms / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format_duration_words(ms) -> str:
    """'1 hour 44 minutes' - for the raid clear-time headline (minute
    precision - see _format_delta for the second-precision comparison)."""
    if not ms or ms < 0:
        return "?"
    total_seconds = int(ms / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m or not h:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if not h and not m:
        parts = [f"{s} second{'s' if s != 1 else ''}"]
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


def _has_section_thumbnail() -> bool:
    return hasattr(discord.ui, "Section") and hasattr(discord.ui, "Thumbnail")


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
        # Registers the Edit button's custom_id so it keeps working across
        # bot restarts - same pattern as announcements.py's draft/published
        # Edit buttons. The actual record lookup happens by the clicked
        # message's ID at click time, not anything baked into this dummy view.
        dummy = discord.ui.LayoutView(timeout=None)
        self._add_edit_action_row(dummy)
        self.bot.add_view(dummy)

    # --- permission check (self-contained, same as other cogs) -----------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    # --- kill/clear-time records ("first kill" + "fastest" badges) -------

    def _get_records(self) -> dict:
        record = self.bot.store.get(RECORDS_KEY)
        if record is None:
            return {"encounters": {}, "clears": {}}
        return {"encounters": record.get("encounters", {}), "clears": record.get("clears", {})}

    def _save_records(self, records: dict):
        self.bot.store.set(RECORDS_KEY, encounters=records["encounters"], clears=records["clears"])

    # --- tier resolution ---------------------------------------------------

    def _resolve_tier(self, tier_name: str) -> dict:
        for tier in (config.CURRENT_TIER, config.PREVIOUS_TIER):
            if tier["name"] == tier_name:
                return tier
        raise ValueError(f"Unknown tier '{tier_name}'.")

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

    def _group_fights_by_encounter(self, fights: list) -> dict:
        groups = {}
        for fight in fights:
            encounter_id = fight.get("encounter_id")
            if encounter_id is None:
                continue  # trash/non-encounter fights don't count toward boss pulls
            groups.setdefault(encounter_id, []).append(fight)
        return groups

    def _build_boss_lines(self, tier: dict, fights_by_encounter: dict, records: dict) -> tuple:
        """
        Returns (lines, newly_killed_ids, new_fastest_kills) where
        new_fastest_kills is {encounter_id: duration_ms} for every boss this
        raid that either had no prior time on record, or beat/tied it -
        the caller persists these after a successful post.
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
                attempts = [f["boss_percentage"] for f in group if f.get("boss_percentage") is not None]
                best = min(attempts) if attempts else None
                pct_text = f", best attempt {best:.1f}% remaining" if best is not None else ""
                lines.append(
                    f"❌ **{boss_name}** — {pulls} pull{'s' if pulls != 1 else ''}, not downed{pct_text}"
                )

        return lines, newly_killed, new_fastest_kills

    def _build_clear_time_line(self, zone_name: str, clear_ms: int, records: dict) -> tuple:
        """Returns (line, new_fastest_clear_ms_or_None) - only meaningful for
        a full clear, so the caller only calls this when clear_status is
        'full_clear'."""
        if not zone_name or not clear_ms:
            return "", None

        clear_words = _format_duration_words(clear_ms)
        prior = records["clears"].get(zone_name, {}).get("fastest_ms")

        if prior is None:
            return f"🕐 **{zone_name} clear time:** {clear_words}", clear_ms

        delta = clear_ms - prior
        if delta < 0:
            return f"🕐 **{zone_name} clear time:** {clear_words} ⚡ **Fastest clear!** ({_format_delta(delta)})", clear_ms
        elif delta == 0:
            return f"🕐 **{zone_name} clear time:** {clear_words} ⚡ **Tied our fastest!**", clear_ms
        else:
            return f"🕐 **{zone_name} clear time:** {clear_words} ({_format_delta(delta)})", None

    def _build_comp_line(self, roster: dict) -> str:
        role_counts = Counter(v["role"] for v in roster.values() if v.get("role"))
        if not roster:
            return ""
        parts = [f"{count} {ROLE_DISPLAY.get(role, role)}" for role, count in role_counts.items()]
        return f"**Roster:** {len(roster)} raiders" + (f" — {', '.join(parts)}" if parts else "")

    def _build_deaths_block(self, deaths: dict) -> str:
        if not deaths:
            return ""
        total = sum(deaths.values())
        top = sorted(deaths.items(), key=lambda kv: -kv[1])[:8]
        lines = [f"💀 **{name}** — {count} death{'s' if count != 1 else ''}" for name, count in top]
        return f"**Fun stats** — {total} total death{'s' if total != 1 else ''}\n" + "\n".join(lines)

    def _build_parses_block(self, parses: list, fights: list) -> str:
        if not parses:
            return ""
        fight_names = {f["id"]: f["name"] for f in fights}
        with_pct = [p for p in parses if p.get("rank_percent") is not None]
        if not with_pct:
            return ""

        lines = []
        mvp = max(with_pct, key=lambda p: p["rank_percent"])
        lines.append(
            f"🏆 **Raid MVP:** {mvp['name']} — {mvp['rank_percent']:.1f}% on "
            f"{fight_names.get(mvp['fight_id'], 'Unknown boss')}"
        )

        elite = sorted(
            (p for p in with_pct if p["rank_percent"] >= config.PARSE_HIGHLIGHT_THRESHOLD),
            key=lambda p: -p["rank_percent"],
        )
        for p in elite[:20]:
            spec_class = " ".join(x for x in (p.get("spec"), p.get("class")) if x)
            suffix = f" ({spec_class})" if spec_class else ""
            lines.append(
                f"🌟 **{p['name']}** — {p['rank_percent']:.1f}% on "
                f"{fight_names.get(p['fight_id'], 'Unknown boss')}{suffix}"
            )
        return "\n".join(lines)

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

    # --- tldr / footer text (rebuilt fresh on every edit, see module docstring) --

    def _build_tldr_text(self, ctx: dict, note: str) -> str:
        text = (
            f"## {ctx['clear_label']} — {ctx['tier_name']} ({ctx['report_date']})\n"
            f"**{ctx['killed_count']}/{ctx['attempted_count']}** bosses downed this raid "
            f"({ctx['total_pulls']} total pulls) · **{ctx['loot_count']}** "
            f"item{'s' if ctx['loot_count'] != 1 else ''} awarded · duration **{ctx['duration_text']}**"
        )
        if ctx.get("clear_time_line"):
            text += f"\n{ctx['clear_time_line']}"
        if note:
            text += f"\n\n*{note.strip()}*"
        return text

    def _build_footer_text(self, ctx: dict, media_link: str) -> str:
        lines = [f"📜 [Full log](https://fresh.warcraftlogs.com/reports/{ctx['report_code']})"]
        if media_link:
            lines.append(media_link.strip())
        return "\n".join(lines)

    # --- block building / pagination ---------------------------------------

    def _text_block(self, content: str) -> dict:
        return {"kind": "text", "content": content, "chars": len(content), "units": 2}

    def _loot_row_block(self, resolved_row: dict) -> dict:
        item = resolved_row["item"]
        quality_emoji = QUALITY_EMOJI.get(item.get("quality"), DEFAULT_QUALITY_EMOJI)
        offspec_tag = " *(OS)*" if resolved_row["offspec"] else ""
        line = f"{quality_emoji} [{item['name']}]({item['wowhead_url']}) → **{resolved_row['character']}**{offspec_tag}"
        if _has_section_thumbnail():
            return {"kind": "loot_row_thumb", "content": line, "icon_url": item["icon_url"], "chars": len(line), "units": 3}
        return {"kind": "text", "content": line, "chars": len(line), "units": 2}

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

    def _add_edit_action_row(self, view: discord.ui.LayoutView):
        action_row = discord.ui.ActionRow()
        edit_button = discord.ui.Button(
            label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id=EDIT_BUTTON_CUSTOM_ID
        )
        edit_button.callback = self._on_edit_click
        action_row.add_item(edit_button)
        view.add_item(action_row)

    def _render_page(self, blocks: list, banner_url: str = None, with_edit_button: bool = False) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_color=discord.Color.blurple())

        if banner_url:
            gallery = discord.ui.MediaGallery(discord.MediaGalleryItem(banner_url))
            container.add_item(gallery)
            container.add_item(discord.ui.Separator())

        for i, block in enumerate(blocks):
            if block["kind"] == "loot_row_thumb":
                section = discord.ui.Section(accessory=discord.ui.Thumbnail(block["icon_url"]))
                section.add_item(discord.ui.TextDisplay(block["content"]))
                container.add_item(section)
            else:
                container.add_item(discord.ui.TextDisplay(block["content"]))
            if i < len(blocks) - 1:
                container.add_item(discord.ui.Separator())

        view.add_item(container)
        if with_edit_button:
            self._add_edit_action_row(view)
        return view

    def _render_pages(self, middle_blocks: list, header_ctx: dict, footer_ctx: dict, note: str,
                       media_link: str, banner_url: str) -> list:
        """Builds the full page list (LayoutViews) from frozen middle blocks
        plus fresh tldr/footer text - used both for the initial post and for
        every edit, so the two never drift apart."""
        blocks = (
            [self._text_block(self._build_tldr_text(header_ctx, note))]
            + middle_blocks
            + [self._text_block(self._build_footer_text(footer_ctx, media_link))]
        )
        pages = self._paginate_blocks(blocks)
        views = []
        for i, page in enumerate(pages):
            is_first, is_last = i == 0, i == len(pages) - 1
            views.append(self._render_page(page, banner_url=banner_url if is_first else None, with_edit_button=is_last))
        return views

    # --- the command ---------------------------------------------------

    @app_commands.command(name="raidsummary", description="Post a raid summary to the raid-summary forum (moderator only)")
    @app_commands.describe(
        tier="Which raid tier was raided",
        report="WCL report link (or bare report code)",
        loot_export="Gargul loot export - the .csv/.txt file from /gargul export",
        clear_status="Full clear or still progressing?",
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
    )
    async def raidsummary(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        report: str,
        loot_export: discord.Attachment,
        clear_status: app_commands.Choice[str],
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

        # --- parse the loot export ---
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

        tier_data = self._resolve_tier(tier.value)
        fights_by_encounter = self._group_fights_by_encounter(summary["fights"])
        records = self._get_records()
        boss_lines, newly_killed_ids, new_fastest_kills = self._build_boss_lines(
            tier_data, fights_by_encounter, records
        )

        killed_count = sum(1 for g in fights_by_encounter.values() if any(f["kill"] for f in g))
        attempted_count = len(fights_by_encounter)
        total_pulls = sum(len(g) for g in fights_by_encounter.values())
        raid_duration_ms = (
            (summary["end_time"] - summary["start_time"])
            if summary["start_time"] and summary["end_time"] else None
        )
        duration_text = _format_duration(raid_duration_ms) if raid_duration_ms else "?"
        clear_label = "🎉 Full Clear!" if clear_status.value == "full_clear" else "Progress Raid"

        report_date = "?"
        if summary["start_time"]:
            report_date = datetime.fromtimestamp(summary["start_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        zone_name = (summary.get("zone") or {}).get("name")
        clear_time_line, new_fastest_clear_ms = "", None
        if clear_status.value == "full_clear" and raid_duration_ms:
            clear_time_line, new_fastest_clear_ms = self._build_clear_time_line(zone_name, raid_duration_ms, records)

        header_ctx = {
            "clear_label": clear_label, "tier_name": tier_data["name"], "report_date": report_date,
            "killed_count": killed_count, "attempted_count": attempted_count, "total_pulls": total_pulls,
            "loot_count": len(resolved_loot), "duration_text": duration_text, "clear_time_line": clear_time_line,
        }
        footer_ctx = {"report_code": report_code}

        middle_blocks = []
        comp_line = self._build_comp_line(summary["roster"])
        if comp_line:
            middle_blocks.append(self._text_block(comp_line))
        if boss_lines:
            middle_blocks.append(self._text_block("**Boss-by-boss**\n" + "\n".join(boss_lines)))
        parses_block = self._build_parses_block(summary["parses"], summary["fights"])
        if parses_block:
            middle_blocks.append(self._text_block(parses_block))
        guild_rank_block = await self._build_guild_rank_block(tier_data, fights_by_encounter)
        if guild_rank_block:
            middle_blocks.append(self._text_block(guild_rank_block))
        deaths_block = self._build_deaths_block(summary["deaths"])
        if deaths_block:
            middle_blocks.append(self._text_block(deaths_block))
        if resolved_loot:
            middle_blocks.append(self._text_block("**Loot**"))
            for row in resolved_loot:
                middle_blocks.append(self._loot_row_block(row))

        banner_url = config.RAID_TIER_BANNERS.get(tier_data["name"]) or None
        page_views = self._render_pages(middle_blocks, header_ctx, footer_ctx, note, media_link, banner_url)

        tag_names = {tier_data["name"].lower(), config.CLEAR_STATUS_TAG_NAMES[clear_status.value].lower()}
        applied_tags = [t for t in forum_channel.available_tags if t.name.lower() in tag_names]

        thread_name = f"{tier_data['name']} — {report_date}"
        try:
            thread_result = await forum_channel.create_thread(
                name=thread_name, view=page_views[0], applied_tags=applied_tags,
            )
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

        # Only commit kill/clear-time records once the post actually succeeded.
        if newly_killed_ids or new_fastest_kills or new_fastest_clear_ms is not None:
            for encounter_id in newly_killed_ids:
                records["encounters"].setdefault(str(encounter_id), {})["first_seen_report"] = report_code
                records["encounters"][str(encounter_id)]["first_seen_date"] = report_date
            for encounter_id, duration_ms in new_fastest_kills.items():
                entry = records["encounters"].setdefault(str(encounter_id), {})
                entry["fastest_ms"] = duration_ms
                entry["fastest_report"] = report_code
                entry["fastest_date"] = report_date
            if new_fastest_clear_ms is not None and zone_name:
                records["clears"][zone_name] = {
                    "fastest_ms": new_fastest_clear_ms, "fastest_report": report_code, "fastest_date": report_date,
                }
            self._save_records(records)

        # Persisted so the ✏️ Edit button can rebuild just the tldr/footer
        # text later without re-touching WCL/Wowhead or the records above -
        # see the module docstring.
        last_message = posted_messages[-1]
        self.bot.store.set(
            last_message.id,
            thread_id=thread.id,
            page_message_ids=[m.id for m in posted_messages],
            middle_blocks=middle_blocks,
            header_ctx=header_ctx,
            footer_ctx=footer_ctx,
            note=note,
            media_link=media_link,
            banner_url=banner_url,
        )

        await interaction.followup.send(f"Posted: {thread.mention}", ephemeral=True)

    # --- editing (note + media link only - see module docstring) -----------

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

        try:
            thread = self.bot.get_channel(record["thread_id"]) or await self.bot.fetch_channel(record["thread_id"])
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send(
                "Couldn't find the original thread - it may have been deleted.", ephemeral=True
            )
            return

        page_views = self._render_pages(
            record["middle_blocks"], record["header_ctx"], record["footer_ctx"],
            note, media_link, record.get("banner_url"),
        )

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
            log.exception("Failed to apply raid summary edit")
            await interaction.followup.send("Something went wrong updating the summary.", ephemeral=True)
            return

        if new_ids[-1] != last_message_id:
            self.bot.store.delete(last_message_id)
        self.bot.store.set(
            new_ids[-1],
            thread_id=record["thread_id"], page_message_ids=new_ids,
            middle_blocks=record["middle_blocks"], header_ctx=record["header_ctx"], footer_ctx=record["footer_ctx"],
            note=note, media_link=media_link, banner_url=record.get("banner_url"),
        )

        await interaction.followup.send("Summary updated.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidSummaryCog(bot))
