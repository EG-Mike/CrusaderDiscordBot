"""
Raid summary feature - posts a presentable per-raid recap (banner, loot with
Wowhead links/icons, boss-by-boss pulls, elite parses, guild rank, "fun
stats", and a link to the full log) as a new thread in a Discord forum
channel, so raiders have somewhere to discuss each raid night.

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
  - Discord's Components V2 caps a single message at ~4000 chars of text
    across all TextDisplay components (and a component-count budget) - loot
    especially can blow past that for a big clear, so the whole summary is
    built as a list of small "blocks" (banner/text/loot-row) which get
    packed into as many separate forum-thread messages as needed - see
    _paginate_blocks(). The first message becomes the thread's OP (with the
    tier + clear-status forum tags applied); the rest are follow-up posts
    in the same thread.
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

KILL_LEDGER_KEY = "raid_summary_kill_ledger"

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
    if not ms or ms < 0:
        return "?"
    total_seconds = int(ms / 1000)
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def _has_section_thumbnail() -> bool:
    return hasattr(discord.ui, "Section") and hasattr(discord.ui, "Thumbnail")


class RaidSummaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.forum_channel_id = int(os.environ["RAID_SUMMARY_FORUM_CHANNEL_ID"])
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.server_slug = os.environ["SERVER_SLUG"]
        self.server_region = os.environ.get("SERVER_REGION", "us")
        self.guild_name = os.environ.get("GUILD_NAME")  # optional - enables the guild-rank section

    # --- permission check (self-contained, same as other cogs) -----------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    # --- kill ledger (for "first kill" badges) ----------------------------

    def _get_kill_ledger(self) -> dict:
        record = self.bot.store.get(KILL_LEDGER_KEY)
        return (record or {}).get("encounters", {})

    def _save_kill_ledger(self, encounters: dict):
        self.bot.store.set(KILL_LEDGER_KEY, encounters=encounters)

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

    def _build_boss_lines(self, tier: dict, fights_by_encounter: dict, ledger: dict) -> tuple:
        """Returns (lines, killed_encounter_ids) - the latter used to update
        the first-kill ledger after a successful post."""
        lines = []
        newly_killed = []
        for boss_name, encounter_id in tier["bosses"].items():
            group = fights_by_encounter.get(encounter_id)
            if not group:
                continue  # not attempted this raid - omit rather than clutter the list
            pulls = len(group)
            kill_fight = next((f for f in group if f["kill"]), None)
            if kill_fight:
                duration = _format_duration((kill_fight["end_time"] or 0) - (kill_fight["start_time"] or 0))
                is_first_kill = str(encounter_id) not in ledger
                badge = " 🆕 **First kill!**" if is_first_kill else ""
                if is_first_kill:
                    newly_killed.append(encounter_id)
                lines.append(
                    f"✅ **{boss_name}** — killed in {pulls} pull{'s' if pulls != 1 else ''} "
                    f"({duration}){badge}"
                )
            else:
                attempts = [f["boss_percentage"] for f in group if f.get("boss_percentage") is not None]
                best = min(attempts) if attempts else None
                pct_text = f", best attempt {best:.1f}% remaining" if best is not None else ""
                lines.append(
                    f"❌ **{boss_name}** — {pulls} pull{'s' if pulls != 1 else ''}, not downed{pct_text}"
                )
        return lines, newly_killed

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

    def _render_page(self, blocks: list, banner_url: str = None) -> discord.ui.LayoutView:
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
        return view

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
        ledger = self._get_kill_ledger()
        boss_lines, newly_killed_ids = self._build_boss_lines(tier_data, fights_by_encounter, ledger)

        killed_count = sum(1 for g in fights_by_encounter.values() if any(f["kill"] for f in g))
        attempted_count = len(fights_by_encounter)
        total_pulls = sum(len(g) for g in fights_by_encounter.values())
        duration_text = _format_duration(
            (summary["end_time"] or 0) - (summary["start_time"] or 0)
        ) if summary["start_time"] and summary["end_time"] else "?"
        clear_label = "🎉 Full Clear!" if clear_status.value == "full_clear" else "Progress Raid"

        report_date = "?"
        if summary["start_time"]:
            report_date = datetime.fromtimestamp(summary["start_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        tldr = (
            f"## {clear_label} — {tier_data['name']} ({report_date})\n"
            f"**{killed_count}/{attempted_count}** bosses downed this raid ({total_pulls} total pulls) · "
            f"**{len(resolved_loot)}** item{'s' if len(resolved_loot) != 1 else ''} awarded · "
            f"duration **{duration_text}**"
        )
        if note:
            tldr += f"\n\n*{note.strip()}*"

        blocks = [self._text_block(tldr)]

        comp_line = self._build_comp_line(summary["roster"])
        if comp_line:
            blocks.append(self._text_block(comp_line))

        if boss_lines:
            blocks.append(self._text_block("**Boss-by-boss**\n" + "\n".join(boss_lines)))

        parses_block = self._build_parses_block(summary["parses"], summary["fights"])
        if parses_block:
            blocks.append(self._text_block(parses_block))

        guild_rank_block = await self._build_guild_rank_block(tier_data, fights_by_encounter)
        if guild_rank_block:
            blocks.append(self._text_block(guild_rank_block))

        deaths_block = self._build_deaths_block(summary["deaths"])
        if deaths_block:
            blocks.append(self._text_block(deaths_block))

        if resolved_loot:
            blocks.append(self._text_block("**Loot**"))
            for row in resolved_loot:
                blocks.append(self._loot_row_block(row))

        footer_lines = [f"📜 [Full log](https://fresh.warcraftlogs.com/reports/{report_code})"]
        if media_link:
            footer_lines.append(media_link.strip())
        blocks.append(self._text_block("\n".join(footer_lines)))

        banner_url = config.RAID_TIER_BANNERS.get(tier_data["name"]) or None
        pages = self._paginate_blocks(blocks)

        tag_names = {tier_data["name"].lower(), config.CLEAR_STATUS_TAG_NAMES[clear_status.value].lower()}
        applied_tags = [t for t in forum_channel.available_tags if t.name.lower() in tag_names]

        thread_name = f"{tier_data['name']} — {report_date}"
        try:
            first_view = self._render_page(pages[0], banner_url=banner_url)
            thread_result = await forum_channel.create_thread(
                name=thread_name, view=first_view, applied_tags=applied_tags,
            )
            thread = thread_result.thread
            for page in pages[1:]:
                await thread.send(view=self._render_page(page))
        except Exception:
            log.exception("Failed to post raid summary to forum")
            await interaction.followup.send(
                "Something went wrong posting the summary - check the bot's permissions on the "
                "raid-summary forum channel (Send Messages, Create Posts, Embed Links).",
                ephemeral=True,
            )
            return

        # Only commit first-kill badges once the post actually succeeded.
        if newly_killed_ids:
            for encounter_id in newly_killed_ids:
                ledger[str(encounter_id)] = {"first_seen_report": report_code, "date": report_date}
            self._save_kill_ledger(ledger)

        await interaction.followup.send(f"Posted: {thread.mention}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidSummaryCog(bot))
