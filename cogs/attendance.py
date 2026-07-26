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
    nickname - the approval flow (cogs/apply.py) already sets this to their
    main character's name on approval, so no separate registration step is
    needed for mains. Alts are linked explicitly via /checkattendance link,
    since only a few people are ever asked to bring a different character.
  - This is a self-contained feature file, same as apply.py/announcements.py -
    add a new cog for the next feature rather than extending this one.
"""

import os
import re
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("wow-apply-bot.attendance")

# Sentinel keys in the shared bot.store (same generic JSON store used
# elsewhere - these aren't real Discord message/application records, just
# reusing the same schema-free set()/get()/update() mechanism).
LOG_LIST_KEY = "attendance_log_list"
ALT_LINKS_KEY = "attendance_alt_links"
EXCLUDED_KEY = "attendance_excluded"
BASELINE_KEY = "attendance_baseline_overrides"
OVERVIEW_MESSAGE_KEY = "attendance_overview_message"

LOG_LIST_MARKER = "attendance-log-list"

REPORT_LINK_RE = re.compile(r"(?:reports/|^)([A-Za-z0-9]{8,20})(?:[/#].*)?$")


def _extract_report_code(link: str) -> str:
    """Accepts a bare report code or a full WCL report URL."""
    link = link.strip().rstrip("/")
    match = REPORT_LINK_RE.search(link)
    return match.group(1) if match else link


class AttendanceCog(commands.Cog):
    checkattendance_group = app_commands.Group(
        name="checkattendance", description="Raid attendance tracking (moderator only)"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.attendance_channel_id = int(os.environ["ATTENDANCE_CHANNEL_ID"])
        self.fresh_role_id = int(os.environ["FRESH_ROLE_ID"])
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])

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

    # --- pinned log-list message -------------------------------------

    def _render_log_list_embed(self, entries: list) -> discord.Embed:
        embed = discord.Embed(
            title="📋 Main Raid Log List",
            description=(
                f"Add with `/checkattendance addlog` · remove with "
                f"`/checkattendance removelog id:<N>`.\n"
                f"The most recent **{config.ATTENDANCE_WINDOW}** entries (marked 🔸) "
                f"are what `/checkattendance run` currently uses."
            ),
            color=discord.Color.blurple(),
        )
        if not entries:
            embed.add_field(name="No logs yet", value="Add one to get started.", inline=False)
        else:
            in_window_ids = {e["id"] for e in entries[-config.ATTENDANCE_WINDOW:]}
            lines = []
            for entry in reversed(entries):  # newest first for display
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
        """Posts (once) or edits in place the pinned log-list message so
        there's always exactly one, growing over time rather than being
        reposted."""
        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            log.warning("ATTENDANCE_CHANNEL_ID set but channel not found/visible")
            return

        record = self._get_log_list()
        embed = self._render_log_list_embed(record["entries"])

        if record.get("pinned_message_id"):
            try:
                message = await channel.fetch_message(record["pinned_message_id"])
                await message.edit(embed=embed)
                return
            except (discord.NotFound, discord.Forbidden):
                pass  # fall through and post/pin a fresh one

        try:
            message = await channel.send(embed=embed)
            await message.pin()
            record["pinned_message_id"] = message.id
            self._save_log_list(record)
        except Exception:
            log.exception("Failed to post/pin the attendance log-list message")

    async def _ensure_log_list_message(self):
        """Startup check - if the log list already has a pinned message
        (tracked in our own storage) we don't need to do anything; if it
        doesn't exist yet at all, post an initial empty one so mods have
        somewhere to look."""
        record = self._get_log_list()
        if record.get("pinned_message_id"):
            return
        await self._sync_log_list_message(None)

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_log_list_message()

    # --- /checkattendance addlog / removelog --------------------------

    @checkattendance_group.command(name="addlog", description="Add a main-raid log to the tracked list")
    @app_commands.describe(
        date="Date of the raid, e.g. 2026-07-29",
        tier="Tier/zone, e.g. SSC/TK",
        link="WCL report link (or bare report code)",
    )
    async def addlog(self, interaction: discord.Interaction, date: str, tier: str, link: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage the log list.", ephemeral=True)
            return

        report_code = _extract_report_code(link)
        record = self._get_log_list()
        next_id = (max((e["id"] for e in record["entries"]), default=0)) + 1
        record["entries"].append({
            "id": next_id,
            "date": date.strip(),
            "tier": tier.strip(),
            "report_code": report_code,
        })
        self._save_log_list(record)
        await self._sync_log_list_message(interaction.guild)

        await interaction.response.send_message(
            f"Added log #{next_id} ({date.strip()} · {tier.strip()}).", ephemeral=True
        )

    @checkattendance_group.command(name="removelog", description="Remove a log from the tracked list by its ID")
    @app_commands.describe(id="The #ID shown next to the log in the pinned list")
    async def removelog(self, interaction: discord.Interaction, id: int):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can manage the log list.", ephemeral=True)
            return

        record = self._get_log_list()
        before = len(record["entries"])
        record["entries"] = [e for e in record["entries"] if e["id"] != id]
        if len(record["entries"]) == before:
            await interaction.response.send_message(f"No log with ID #{id} found.", ephemeral=True)
            return

        self._save_log_list(record)
        await self._sync_log_list_message(interaction.guild)
        await interaction.response.send_message(f"Removed log #{id}.", ephemeral=True)

    # --- /checkattendance link / exclude / include --------------------

    @checkattendance_group.command(name="link", description="Link an alt character to a main, for attendance purposes")
    @app_commands.describe(main="The main character's name", alt="The alt character's name")
    async def link(self, interaction: discord.Interaction, main: str, alt: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can link characters.", ephemeral=True)
            return

        links = self._get_alt_links()
        links[alt.strip().lower()] = main.strip()
        self._save_alt_links(links)

        await interaction.response.send_message(
            f"Linked **{alt.strip()}** as an alt of **{main.strip()}**.", ephemeral=True
        )

    @checkattendance_group.command(name="exclude", description="Excuse a player from attendance tracking")
    @app_commands.describe(name="Their main character's name")
    async def exclude(self, interaction: discord.Interaction, name: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can exclude players.", ephemeral=True)
            return

        excluded = self._get_excluded()
        name = name.strip()
        if not any(n.lower() == name.lower() for n in excluded):
            excluded.append(name)
            self._save_excluded(excluded)

        await interaction.response.send_message(f"**{name}** is now excused from attendance tracking.", ephemeral=True)

    @checkattendance_group.command(name="include", description="Resume attendance tracking for a previously excused player")
    @app_commands.describe(name="Their main character's name")
    async def include(self, interaction: discord.Interaction, name: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can re-include players.", ephemeral=True)
            return

        name = name.strip()
        excluded = self._get_excluded()
        excluded = [n for n in excluded if n.lower() != name.lower()]
        self._save_excluded(excluded)

        overrides = self._get_baseline_overrides()
        overrides[name.lower()] = config.ATTENDANCE_INCLUDE_BASELINE
        self._save_baseline_overrides(overrides)

        await interaction.response.send_message(
            f"**{name}** is back on attendance tracking - assumed "
            f"{config.ATTENDANCE_INCLUDE_BASELINE}/{config.ATTENDANCE_WINDOW} for the next "
            "`/checkattendance run` only, then real log data takes over again.",
            ephemeral=True,
        )

    # --- /checkattendance run -----------------------------------------

    async def _compute_attendance(self, guild: discord.Guild) -> dict:
        record = self._get_log_list()
        window_entries = record["entries"][-config.ATTENDANCE_WINDOW:]

        rosters = []  # oldest-first, one lowercase-name set per log in the window
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

            main_name = member.display_name
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
            self._save_baseline_overrides({})  # one-shot - consumed after this run

        return {
            "promote": promote,
            "demote": demote,
            "watch_promote": watch_promote,
            "watch_demote": watch_demote,
            "excluded": excluded_display,
            "window_size": window_size,
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
        embed.add_field(
            name="🚫 Excluded",
            value=", ".join(results["excluded"]) or "None",
            inline=False,
        )
        embed.set_footer(text="Role changes are manual - this is a discussion aid, not an automatic action.")
        return embed

    @checkattendance_group.command(name="run", description="Generate the attendance overview")
    async def run(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can run attendance checks.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = self.bot.get_channel(self.attendance_channel_id)
        if channel is None:
            await interaction.followup.send("Couldn't find the attendance channel.", ephemeral=True)
            return

        results = await self._compute_attendance(interaction.guild)
        embed = self._render_overview_embed(results)

        # Delete-and-repost so only one overview is ever visible at a time.
        record = self.bot.store.get(OVERVIEW_MESSAGE_KEY)
        if record and record.get("message_id"):
            try:
                old_message = await channel.fetch_message(record["message_id"])
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        new_message = await channel.send(embed=embed)
        self.bot.store.set(OVERVIEW_MESSAGE_KEY, message_id=new_message.id)

        await interaction.followup.send("Overview posted.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AttendanceCog(bot))