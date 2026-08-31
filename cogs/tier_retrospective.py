"""
End-of-tier retrospective - a one-time-per-tier (~every 15-20 weeks) fun
stats recap, posted as an editable draft (same sandbox-channel draft/edit/
publish flow as cogs/announcements.py) rather than straight to a public
channel.

Design, per discussion:
  - Self-contained like every other cog here (see raid_summary.py's/
    announcements.py's own docstrings) - this is a NEW cog file rather
    than more code bolted onto either of those, even though it leans on
    both: cogs/raid_summary.py's persisted per-tier report list
    (get_tier_reports()) is the input, and this cog's own draft/edit/
    publish mechanics mirror cogs/announcements.py's (same idea: a
    moderator-only command creates a draft in the sandbox channel with
    Edit/Publish buttons, Publish opens a channel picker and reposts the
    final version there).
  - ALL data comes from WCL responses already cached by every report's
    original /raidsummary post (get_report_summary/get_report_role_composition/
    get_report_boss_only_totals - see wcl_client.py) - this command makes
    ZERO new WCL API calls of its own. That's why raid_summary.py went out
    of its way to bank boss-only damage/healing and raw overheal numbers
    ahead of time (see its _assemble_and_post_summary) - so this could be
    built entirely against cache, no matter how many reports the tier
    ends up having. "Most Loot Received" is the one stat NOT sourced from
    WCL at all (WCL has no reliable per-item loot-award event - see
    gargul_loot.py's own docstring) - it reads each report's raw Gargul
    loot rows straight off raid_summary.py's own post record
    (get_loot_rows_for_report), still zero new network calls either way.
    Older reports posted before that field was persisted just contribute
    no loot data (not an error) - see that method's docstring, which is
    also why this naturally only ever covers the CURRENT tier onward and
    never SSC/TK's history: those reports were posted before loot_rows
    existed at all.
  - Only raid_type == "main" reports count (get_tier_reports's default) -
    an interactively-posted alt/fun raid for the same tier shouldn't
    pollute tier-wide records like fastest clear, attendance, or the
    unique-roster count.
  - "Medal" = per-raid leaderboard POSITION (confirmed with the user,
    2026-08): for each raid night, whoever's #1 in that raid's own
    Activity%/Potions/Interrupts/Dispels/Damage/Healing/Overheal%
    leaderboard (the same 7 stats cogs/raid_summary.py's own per-raid
    summary already ranks - everything except the Deaths leaderboard,
    deliberately excluded since being #1 there means dying the most, not
    an achievement) gets gold for that raid/stat, #2 silver, #3 bronze -
    summed across every raid in the tier. See _aggregate's _award() helper.
  - "Top 5 healing (raw, incl. overheal)" vs "...effective, overheal
    subtracted": WCL's own healing_done already IS the effective (post-
    overheal) number (see wcl_client._fetch_healing_done) - so the
    "effective" stat is just a tier-wide sum of that, and the "raw"
    (gross throughput) stat is healing_done + overheal_raw summed
    instead. Both requested stats turn out to need the exact same two
    underlying numbers per player, just combined differently.
  - Editing: unlike a hand-typed /announce, this content is entirely
    computed - there's no sensible "rewrite this stat line" free-text
    edit surface. What IS editable: an optional note (prepended, e.g. a
    guild-specific shoutout) via a small modal, and a 🔄 Regenerate button
    that re-aggregates from scratch (useful if one more report gets
    posted for the tier between drafting and publishing) while keeping
    the note. Same "iterate on the real rendered message, not a preview"
    philosophy announcements.py's docstring describes.
  - Pagination mirrors cogs/raid_summary.py's own block/page approach
    (small self-contained copy here, not a cross-cog call, to keep this
    file self-contained per the stated convention) - one Components V2
    LayoutView per Discord message, as many messages as the content needs.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
import icons
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.tier_retrospective")

RANK_MEDALS = ["🥇", "🥈", "🥉"]
OTHER_RANK_BULLET = "🔹"

# The 7 per-raid leaderboards that count toward medals - same stats
# cogs/raid_summary.py ranks per raid, Deaths deliberately excluded (see
# module docstring). Each is (summary_key, direction, healers_only).
MEDAL_STATS = [
    ("activity", "high", False),
    ("potion_casts", "high", False),
    ("interrupts", "high", False),
    ("dispels", "high", False),
    ("damage_done", "high", False),
    ("healing_done", "high", False),
    ("overheal_pct", "low", True),
]

MAX_CHARS_PER_PAGE = 3500
MAX_UNITS_PER_PAGE = 24

DRAFT_PUBLISH_WAIT_SECONDS = 310


def _name_icon(guild: discord.Guild, class_name) -> str:
    if not class_name:
        return ""
    icon = icons.resolve_class_icon(guild, class_name)
    return f"{icon} " if icon else ""


def _format_duration_words(ms) -> str:
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


def _group_fights_by_encounter(fights: list) -> dict:
    """Same filtering cogs/raid_summary.py's own _group_fights_by_encounter
    uses - duplicated here (small, pure, no shared state) rather than a
    cross-cog call, to keep this file self-contained."""
    groups = {}
    for fight in fights:
        encounter_id = fight.get("encounter_id")
        if encounter_id is None:
            continue
        if not fight["kill"] and (fight.get("boss_percentage") or 0) >= 100:
            continue
        groups.setdefault(encounter_id, []).append(fight)
    return groups


def _report_span(summary: dict) -> dict:
    """Returns {"date": 'YYYY-MM-DD'|None, "first_pull_ms": abs epoch ms|None,
    "last_kill_ms": abs epoch ms|None} - "first pull" is the first REAL
    boss pull and "last kill" is the last KILL fight's end (not the
    report's own end time, which can run past the last kill for looting/
    chat) - same reasoning cogs/raid_summary.py's own duration line uses,
    but anchored to the last kill instead of report end specifically for
    the "fastest raid night" stat."""
    fights = summary.get("fights") or []
    encounter_fights = [f for f in fights if f.get("encounter_id") is not None]
    first_pull_rel = min(
        (f["start_time"] for f in encounter_fights if f["start_time"] is not None), default=None
    )
    kill_fights = [f for f in fights if f.get("kill")]
    last_kill_rel = max(
        (f["end_time"] for f in kill_fights if f["end_time"] is not None), default=None
    )
    if summary.get("start_time") is None or first_pull_rel is None:
        return {"date": None, "first_pull_ms": None, "last_kill_ms": None}
    first_pull_abs = summary["start_time"] + first_pull_rel
    last_kill_abs = summary["start_time"] + last_kill_rel if last_kill_rel is not None else None
    date = datetime.fromtimestamp(first_pull_abs / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return {"date": date, "first_pull_ms": first_pull_abs, "last_kill_ms": last_kill_abs}


class TierRetrospectiveModal(discord.ui.Modal, title="Retrospective Note"):
    note = discord.ui.TextInput(
        label="Note/shoutout (optional)", style=discord.TextStyle.paragraph,
        required=False, max_length=1000,
    )

    def __init__(self, cog: "TierRetrospectiveCog", last_message_id: int, prefill_note=None):
        super().__init__()
        self.cog = cog
        self.last_message_id = last_message_id
        if prefill_note:
            self.note.default = prefill_note

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog._apply_note(interaction, self.last_message_id, str(self.note).strip() or None)


class TierRetrospectiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.sandbox_channel_id = os.environ.get("ANNOUNCEMENT_SANDBOX_CHANNEL_ID")
        # Own dedicated store, same "one small file per subsystem" reasoning
        # raid_summary.py's own split from applications.json used.
        self.store = ApplicationStore(path="tier_retrospective_store.json")

    # --- permission check (self-contained, same as other cogs) -----------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    # --- aggregation ---------------------------------------------------

    async def _aggregate(self, tier_data: dict, raid_cog) -> dict:
        """
        Walks every main-raid report on record for this tier (see
        get_tier_reports) and builds every stat this feature needs, all
        from already-cached WCL responses. Returns None if the tier has no
        posted main-raid reports yet.
        """
        tier_name = tier_data["name"]
        entries = raid_cog.get_tier_report_entries(tier_name, raid_type="main")
        session_by_code = {e["code"]: e["session_id"] for e in entries}
        codes = [e["code"] for e in entries]
        wcl = self.bot.wcl

        reports = []
        for code in codes:
            try:
                summary = await wcl.get_report_summary(code)
            except Exception:
                log.warning("Retrospective: couldn't fetch report %s - skipping", code, exc_info=True)
                continue
            if not summary.get("fights"):
                continue
            span = _report_span(summary)
            if span["date"] is None:
                continue
            fights_by_encounter = _group_fights_by_encounter(summary["fights"])
            boss_fight_ids = [
                f["id"] for eid in tier_data["bosses"].values() for f in fights_by_encounter.get(eid, [])
            ]
            try:
                boss_only = await wcl.get_report_boss_only_totals(code, boss_fight_ids)
            except Exception:
                log.warning("Retrospective: boss-only totals failed for %s", code, exc_info=True)
                boss_only = {}
            try:
                composition = await wcl.get_report_role_composition(code) or {}
            except Exception:
                log.warning("Retrospective: role composition failed for %s", code, exc_info=True)
                composition = {}
            reports.append({
                "code": code, "summary": summary, "span": span,
                "fights_by_encounter": fights_by_encounter, "boss_only": boss_only,
                "composition": composition, "session_id": session_by_code.get(code, code),
                # Not a WCL response like everything else gathered here (see
                # the module docstring's "zero new WCL calls" design) -
                # raid_cog's own store, already loaded, so still free. Older
                # reports posted before loot_rows started being persisted
                # (see get_loot_rows_for_report's docstring) just come back
                # [] - their loot silently doesn't count, same as this
                # feature not existing for them, rather than erroring.
                "loot_rows": raid_cog.get_loot_rows_for_report(code),
            })

        if not reports:
            return None

        reports.sort(key=lambda r: (r["span"]["date"], r["code"]))

        # Group reports into "sessions" (raid weeks) - normally 1:1 with a
        # report, but a moderator can fold two or more reports into one
        # session via raid_cog.merge_tier_reports() for a week split
        # across multiple calendar nights (e.g. SSC cleared one night, TK
        # cleared a different night the same week - see that method's
        # docstring). Every report's WEEK NUMBER comes from its session,
        # never the report itself, so merged reports always land on the
        # same week.
        sessions = {}
        for r in reports:
            sessions.setdefault(r["session_id"], []).append(r)

        def _session_date(members):
            return min(m["span"]["date"] for m in members)

        session_ids_sorted = sorted(sessions.keys(), key=lambda sid: (_session_date(sessions[sid]), sid))
        week_by_session = {sid: i for i, sid in enumerate(session_ids_sorted, start=1)}
        for r in reports:
            r["week"] = week_by_session[r["session_id"]]

        classes_map = {}
        for r in reports:
            classes_map.update((r["composition"] or {}).get("classes") or {})

        # --- guild-wide -------------------------------------------------

        sub_instances = config.TIER_SUB_INSTANCES.get(tier_name) or {tier_name: list(tier_data["bosses"].keys())}
        fastest_clears = {}
        fastest_raidnight = None
        unique_chars = set()
        total_boss_kills = 0
        total_raid_time_ms = 0

        for r in reports:
            fbe = r["fights_by_encounter"]
            comp = r["composition"] or {}
            unique_chars |= set(comp.get("tanks") or []) | set(comp.get("healers") or []) | set(comp.get("dps") or [])

            for eid in tier_data["bosses"].values():
                if any(f["kill"] for f in fbe.get(eid, [])):
                    total_boss_kills += 1

            if r["span"]["last_kill_ms"] is not None:
                night_ms = r["span"]["last_kill_ms"] - r["span"]["first_pull_ms"]
                if night_ms > 0:
                    total_raid_time_ms += night_ms

            for instance_name, boss_names in sub_instances.items():
                encounter_ids = [tier_data["bosses"][n] for n in boss_names if n in tier_data["bosses"]]
                groups = [fbe.get(eid) for eid in encounter_ids]
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
                prior = fastest_clears.get(instance_name)
                if prior is None or duration_ms < prior["ms"]:
                    fastest_clears[instance_name] = {"ms": duration_ms, "week": r["week"], "date": r["span"]["date"]}

        # Fastest raid night is measured per SESSION (see grouping above),
        # not per individual WCL report - when a week's clear is split
        # across two reports (SSC one night, TK another), the correct
        # comparison is the SUM of each member report's own first-pull-to-
        # last-kill span, never a min-to-max across both reports' absolute
        # timestamps (which would wrongly count the gap between the two
        # nights - e.g. a full day - as raid time). For an un-merged
        # session (the common case, one report = one week) this reduces to
        # exactly that report's own span, same as before.
        for sid, members in sessions.items():
            member_spans_ms = [
                m["span"]["last_kill_ms"] - m["span"]["first_pull_ms"] for m in members
                if m["span"]["last_kill_ms"] is not None and m["span"]["first_pull_ms"] is not None
                and m["span"]["last_kill_ms"] > m["span"]["first_pull_ms"]
            ]
            if not member_spans_ms:
                continue
            session_ms = sum(member_spans_ms)
            if fastest_raidnight is None or session_ms < fastest_raidnight["ms"]:
                fastest_raidnight = {"ms": session_ms, "week": week_by_session[sid], "date": _session_date(members)}

        # --- personal: medals ---------------------------------------------

        medal_totals = {}

        def _award(values: dict, direction: str):
            if not values:
                return
            ranked = sorted(values.items(), key=lambda kv: kv[1], reverse=(direction != "low"))[:3]
            for medal, (name, _val) in zip(("gold", "silver", "bronze"), ranked):
                bucket = medal_totals.setdefault(name, {"gold": 0, "silver": 0, "bronze": 0})
                bucket[medal] += 1

        for r in reports:
            s = r["summary"]
            comp = r["composition"] or {}
            healer_names = set(comp.get("healers") or [])
            for key, direction, healers_only in MEDAL_STATS:
                values = s.get(key) or {}
                if healers_only:
                    values = {n: v for n, v in values.items() if n in healer_names}
                _award(values, direction)

        # --- personal: tier-wide sums ---------------------------------------

        damage_all, damage_boss, healing_net, healing_gross = {}, {}, {}, {}
        deaths_total, potion_totals, attendance_counts, loot_counts = {}, {}, {}, {}

        for r in reports:
            s = r["summary"]
            for name, v in (s.get("damage_done") or {}).items():
                damage_all[name] = damage_all.get(name, 0) + v
            for name, v in ((r["boss_only"] or {}).get("damage_done") or {}).items():
                damage_boss[name] = damage_boss.get(name, 0) + v
            for name, v in (s.get("healing_done") or {}).items():
                healing_net[name] = healing_net.get(name, 0) + v
            for name, v in (s.get("overheal_raw") or {}).items():
                healing_gross[name] = healing_gross.get(name, 0) + v
            for name, v in (s.get("deaths") or {}).items():
                deaths_total[name] = deaths_total.get(name, 0) + v
            for name, v in (s.get("potion_casts") or {}).items():
                potion_totals[name] = potion_totals.get(name, 0) + v
            # Every row counts as 1 item toward its winner, mainspec or off
            # - not attributed to whoever's tracking loot, so a disenchant
            # winner name (whatever the guild's Gargul setup records that
            # as) shows up here like any other "character" would, same as
            # cogs/raid_summary.py's own per-raid loot list never special-
            # cases it either.
            for row in r["loot_rows"]:
                character = row.get("character")
                if character:
                    loot_counts[character] = loot_counts.get(character, 0) + 1

        # Attendance is counted per SESSION (raid week), not per report -
        # a raider who only made SSC's night in a week split across two
        # reports (see session grouping above) still attended that WEEK,
        # and shouldn't be double-counted relative to raiders in a normal
        # un-merged week just because their week happens to have 2 reports.
        #
        # Names are resolved through the SAME alt->main links
        # /checkattendance link maintains (cogs/attendance.py's
        # ALT_LINKS_KEY, via get_alt_links()) before counting - a raider
        # who played their main one week and an alt another week is one
        # attendee, not two fractional ones. Deliberately attendance-only
        # (confirmed with the moderator, 2026-08): every OTHER per-player
        # stat here (damage/healing/deaths/potions/medals) stays keyed by
        # raw character name, and unique_chars above is deliberately raw
        # character identity too - that one exists specifically to catch
        # people tagging along on alts (see config.EXCLUDED_ENCOUNTER_IDS's
        # docstring), so collapsing it through alt links would defeat its
        # own purpose.
        attendance_cog = self.bot.get_cog("AttendanceCog")
        alt_links = attendance_cog.get_alt_links() if attendance_cog else {}

        for members in sessions.values():
            attendees = set()
            for m in members:
                for name, kills in (m["summary"].get("kill_counts") or {}).items():
                    if kills >= 1:
                        attendees.add(alt_links.get(name.lower(), name))
            for name in attendees:
                attendance_counts[name] = attendance_counts.get(name, 0) + 1

        for name, v in healing_net.items():
            healing_gross[name] = healing_gross.get(name, 0) + v

        return {
            "tier_name": tier_name,
            "total_weeks": len(sessions),
            "fastest_clears": fastest_clears,
            "fastest_raidnight": fastest_raidnight,
            "unique_chars": unique_chars,
            "total_boss_kills": total_boss_kills,
            "total_raid_time_ms": total_raid_time_ms,
            "classes": classes_map,
            "medal_totals": medal_totals,
            "damage_all": damage_all,
            "damage_boss": damage_boss,
            "healing_net": healing_net,
            "healing_gross": healing_gross,
            "deaths_total": deaths_total,
            "potion_totals": potion_totals,
            "attendance_counts": attendance_counts,
            "loot_counts": loot_counts,
        }

    # --- rendering -------------------------------------------------------

    def _build_top_block(self, title: str, emoji: str, values: dict, classes_map: dict,
                          guild: discord.Guild, value_fmt, top_n: int = 5) -> str:
        if not values:
            return ""
        top = sorted(values.items(), key=lambda kv: -kv[1])[:top_n]
        lines = [
            f"{RANK_MEDALS[i] if i < 3 else OTHER_RANK_BULLET} "
            f"{_name_icon(guild, classes_map.get(name))}**{name}** — {value_fmt(v)}"
            for i, (name, v) in enumerate(top)
        ]
        return f"{emoji} **{title}**\n" + "\n".join(lines)

    def _build_title_block(self, agg: dict) -> str:
        return f"## 🏆 {agg['tier_name']} — Tier Recap"

    def _build_stats_block(self, agg: dict) -> str:
        lines = [f"📆 **{agg['total_weeks']}** raid night{'s' if agg['total_weeks'] != 1 else ''} logged this tier."]
        for instance_name, info in agg["fastest_clears"].items():
            lines.append(
                f"⏱️ Fastest **{instance_name}** clear: **{_format_duration_words(info['ms'])}** "
                f"(Week {info['week']}, {info['date']})"
            )
        if agg["fastest_raidnight"]:
            fr = agg["fastest_raidnight"]
            lines.append(
                f"⚡ Fastest raid night (first pull → last kill): **{_format_duration_words(fr['ms'])}** "
                f"(Week {fr['week']}, {fr['date']})"
            )
        lines.append(f"👥 **{len(agg['unique_chars'])}** unique character{'s' if len(agg['unique_chars']) != 1 else ''} raided this tier.")
        lines.append(f"💀 **{agg['total_boss_kills']}** total boss kill{'s' if agg['total_boss_kills'] != 1 else ''} logged.")
        lines.append(f"🕐 **{_format_duration_words(agg['total_raid_time_ms'])}** of combined raid time.")
        return "\n".join(lines)

    def _build_medal_blocks(self, agg: dict, guild: discord.Guild) -> list:
        totals = agg["medal_totals"]
        if not totals:
            return []
        classes_map = agg["classes"]
        by_gold = sorted(totals.items(), key=lambda kv: -kv[1]["gold"])[:3]
        by_total = sorted(totals.items(), key=lambda kv: -(kv[1]["gold"] + kv[1]["silver"] + kv[1]["bronze"]))[:3]

        gold_lines = [
            f"{RANK_MEDALS[i]} {_name_icon(guild, classes_map.get(name))}**{name}** — {t['gold']} 🥇"
            for i, (name, t) in enumerate(by_gold)
        ]
        total_lines = [
            f"{RANK_MEDALS[i]} {_name_icon(guild, classes_map.get(name))}**{name}** — "
            f"{t['gold'] + t['silver'] + t['bronze']} total ({t['gold']}🥇 {t['silver']}🥈 {t['bronze']}🥉)"
            for i, (name, t) in enumerate(by_total)
        ]
        return [
            "**🥇 Most Gold Medals** *(medals earned by topping a raid night's Activity%/Potions/"
            "Interrupts/Dispels/Damage/Healing/Overheal% leaderboard)*\n" + "\n".join(gold_lines),
            "**🏅 Most Medals Overall**\n" + "\n".join(total_lines),
        ]

    def _build_attendance_block(self, agg: dict, guild: discord.Guild) -> str:
        counts = agg["attendance_counts"]
        if not counts:
            return ""
        classes_map = agg["classes"]
        by_count = {}
        for name, c in counts.items():
            by_count.setdefault(c, []).append(name)
        distinct_counts = sorted(by_count.keys(), reverse=True)[:5]
        lines = []
        for rank, count in enumerate(distinct_counts, start=1):
            names = sorted(by_count[count])
            names_text = ", ".join(f"{_name_icon(guild, classes_map.get(n))}**{n}**" for n in names)
            lines.append(f"**#{rank} ({count}/{agg['total_weeks']} raids):** {names_text}")
        return "📅 **Attendance**\n" + "\n".join(lines)

    def _build_all_blocks(self, agg: dict, guild: discord.Guild, note: str) -> list:
        # Note/shoutout sits right below the title and above the stats -
        # see _apply_note, which re-inserts it at the same position
        # (index 1) when edited later.
        blocks = [self._build_title_block(agg)]
        if note:
            blocks.append(f"*{note}*")
        blocks.append(self._build_stats_block(agg))
        blocks.extend(self._build_medal_blocks(agg, guild))

        classes_map = agg["classes"]
        potions_block = self._build_top_block(
            "Top 3 Potions Used (Destruction + Haste)", "🧪", agg["potion_totals"], classes_map, guild,
            lambda v: f"{int(v)} used", top_n=3,
        )
        if potions_block:
            blocks.append(potions_block)

        damage_all_block = self._build_top_block(
            "Top 5 Damage Done (bosses + trash)", "⚔️", agg["damage_all"], classes_map, guild,
            lambda v: f"{int(v):,}", top_n=5,
        )
        if damage_all_block:
            blocks.append(damage_all_block)

        damage_boss_block = self._build_top_block(
            "Top 5 Damage Done (bosses only)", "🗡️", agg["damage_boss"], classes_map, guild,
            lambda v: f"{int(v):,}", top_n=5,
        )
        if damage_boss_block:
            blocks.append(damage_boss_block)

        healing_gross_block = self._build_top_block(
            "Top 5 Healing Done (raw, incl. overheal)", "💦", agg["healing_gross"], classes_map, guild,
            lambda v: f"{int(v):,}", top_n=5,
        )
        if healing_gross_block:
            blocks.append(healing_gross_block)

        healing_net_block = self._build_top_block(
            "Top 5 Healing Done (effective, overheal subtracted)", "💚", agg["healing_net"], classes_map, guild,
            lambda v: f"{int(v):,}", top_n=5,
        )
        if healing_net_block:
            blocks.append(healing_net_block)

        loot_block = self._build_top_block(
            "Most Loot Received", "🎁", agg["loot_counts"], classes_map, guild,
            lambda v: f"{int(v)} item{'s' if int(v) != 1 else ''}", top_n=5,
        )
        if loot_block:
            blocks.append(loot_block)

        attendance_block = self._build_attendance_block(agg, guild)
        if attendance_block:
            blocks.append(attendance_block)

        deaths_block = self._build_top_block(
            "Most Deaths (tier total)", "💀", agg["deaths_total"], classes_map, guild,
            lambda v: f"{int(v)} death{'s' if int(v) != 1 else ''}", top_n=10,
        )
        if deaths_block:
            blocks.append(deaths_block)

        return blocks

    # --- pagination (small self-contained copy - see module docstring) ---

    def _text_block(self, content: str) -> dict:
        return {"content": content, "chars": len(content), "units": 2}

    def _paginate_blocks(self, blocks: list) -> list:
        pages, current, chars, units = [], [], 0, 0
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

    def _render_page(self, blocks: list, footer_text: str = None, buttons: str = None) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_color=discord.Color.gold())
        for i, block in enumerate(blocks):
            container.add_item(discord.ui.TextDisplay(block["content"]))
            if i < len(blocks) - 1:
                container.add_item(discord.ui.Separator())
        view.add_item(container)
        if footer_text:
            view.add_item(discord.ui.TextDisplay(footer_text))
        if buttons == "draft":
            self._add_draft_buttons(view)
        elif buttons == "published_marker":
            self._add_published_marker_buttons(view)
        return view

    def _render_pages(self, raw_blocks: list, destination: str, published_channel_id: int = None,
                       published_by: str = None) -> list:
        """
        destination controls both the footer and which buttons (if any)
        land on the last page:
          - "draft": unpublished sandbox draft - "📝 Draft" footer, full
            Edit Note/Regenerate/Publish button row.
          - "published_marker": the sandbox draft AFTER publishing - "✅
            Published to #channel" footer, Edit Note + Regenerate (no
            Publish - mirrors announcements.py's
            _render_published_draft_marker, except Regenerate stays live
            here since a re-aggregation can legitimately need to reach an
            already-published recap too, e.g. a stat-calculation fix -
            see _on_regenerate_click, which re-syncs the live public post
            AND this marker together when clicked from this state).
          - "published": the actual public copy posted to the target
            channel - no footer, no buttons at all (public audience, not
            just moderators - mirrors announcements.py's
            _render_posted_announcement).
        """
        text_blocks = [self._text_block(b) for b in raw_blocks if b]
        pages = self._paginate_blocks(text_blocks)
        if not pages:
            pages = [[]]

        if destination == "draft":
            footer, buttons = "-# 📝 Draft - not yet published", "draft"
        elif destination == "published_marker":
            footer = f"-# ✅ Published to <#{published_channel_id}> by {published_by}"
            buttons = "published_marker"
        else:
            footer, buttons = None, None

        views = []
        for i, page in enumerate(pages):
            is_last = i == len(pages) - 1
            views.append(
                self._render_page(page, footer_text=footer if is_last else None, buttons=buttons if is_last else None)
            )
        return views

    def _add_draft_buttons(self, view: discord.ui.LayoutView):
        action_row = discord.ui.ActionRow()
        note_button = discord.ui.Button(
            label="✏️ Edit Note", style=discord.ButtonStyle.secondary, custom_id="tier_retro_note_btn"
        )
        note_button.callback = self._on_note_click
        action_row.add_item(note_button)
        regen_button = discord.ui.Button(
            label="🔄 Regenerate", style=discord.ButtonStyle.secondary, custom_id="tier_retro_regen_btn"
        )
        regen_button.callback = self._on_regenerate_click
        action_row.add_item(regen_button)
        publish_button = discord.ui.Button(
            label="🚀 Publish", style=discord.ButtonStyle.success, custom_id="tier_retro_publish_btn"
        )
        publish_button.callback = self._on_publish_click
        action_row.add_item(publish_button)
        view.add_item(action_row)

    def _add_published_marker_buttons(self, view: discord.ui.LayoutView):
        action_row = discord.ui.ActionRow()
        note_button = discord.ui.Button(
            label="✏️ Edit Note", style=discord.ButtonStyle.secondary, custom_id="tier_retro_note_btn"
        )
        note_button.callback = self._on_note_click
        action_row.add_item(note_button)
        regen_button = discord.ui.Button(
            label="🔄 Regenerate", style=discord.ButtonStyle.secondary, custom_id="tier_retro_regen_btn"
        )
        regen_button.callback = self._on_regenerate_click
        action_row.add_item(regen_button)
        view.add_item(action_row)

    async def cog_load(self):
        # Registers every button's custom_id so they keep working across
        # bot restarts - same pattern raid_summary.py/announcements.py
        # already use. The actual record lookup happens by clicked message
        # ID at click time, not anything baked into these dummy views.
        draft_dummy = discord.ui.LayoutView(timeout=None)
        self._add_draft_buttons(draft_dummy)
        self.bot.add_view(draft_dummy)

        marker_dummy = discord.ui.LayoutView(timeout=None)
        self._add_published_marker_buttons(marker_dummy)
        self.bot.add_view(marker_dummy)

    # --- reconciling a page count change (edit/regenerate) ----------------

    async def _reconcile_pages(self, channel, old_ids: list, page_views: list) -> list:
        new_ids = []
        for i, view in enumerate(page_views):
            if i < len(old_ids):
                try:
                    message = await channel.fetch_message(old_ids[i])
                    await message.edit(view=view)
                    new_ids.append(message.id)
                    continue
                except (discord.NotFound, discord.Forbidden):
                    pass
            new_ids.append((await channel.send(view=view)).id)

        for stale_id in old_ids[len(page_views):]:
            try:
                stale_message = await channel.fetch_message(stale_id)
                await stale_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        return new_ids

    # --- the command -----------------------------------------------------

    @app_commands.command(
        name="tier-recap",
        description="Draft an end-of-tier stats recap in the sandbox channel (moderator only)",
    )
    @app_commands.describe(tier="Which tier to summarize")
    @app_commands.choices(
        tier=[
            app_commands.Choice(name=config.CURRENT_TIER["name"], value=config.CURRENT_TIER["name"]),
            app_commands.Choice(name=config.PREVIOUS_TIER["name"], value=config.PREVIOUS_TIER["name"]),
        ],
    )
    async def tier_retrospective(self, interaction: discord.Interaction, tier: str):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can draft a tier retrospective.", ephemeral=True)
            return
        if not self.sandbox_channel_id:
            await interaction.response.send_message(
                "No sandbox channel is configured - set ANNOUNCEMENT_SANDBOX_CHANNEL_ID in the bot's .env first.",
                ephemeral=True,
            )
            return
        sandbox_channel = self.bot.get_channel(int(self.sandbox_channel_id))
        if sandbox_channel is None:
            await interaction.response.send_message(
                "Couldn't find the configured sandbox channel.", ephemeral=True
            )
            return
        raid_cog = self.bot.get_cog("RaidSummaryCog")
        if raid_cog is None:
            await interaction.response.send_message("RaidSummaryCog isn't loaded.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        tier_data = config.CURRENT_TIER if tier == config.CURRENT_TIER["name"] else config.PREVIOUS_TIER
        agg = await self._aggregate(tier_data, raid_cog)
        if agg is None:
            await interaction.followup.send(
                f"No main-raid reports are on record for **{tier}** yet - nothing to summarize.",
                ephemeral=True,
            )
            return

        raw_blocks = self._build_all_blocks(agg, interaction.guild, note=None)
        page_views = self._render_pages(raw_blocks, destination="draft")

        messages = []
        for view in page_views:
            messages.append(await sandbox_channel.send(view=view))

        last_message = messages[-1]
        self.store.set(
            last_message.id,
            tier_name=tier_data["name"],
            page_message_ids=[m.id for m in messages],
            raw_blocks=raw_blocks,
            note=None,
            published=False,
        )

        await interaction.followup.send(
            f"Draft posted in {sandbox_channel.mention} ({len(messages)} message"
            f"{'s' if len(messages) != 1 else ''}) - tweak it there, then hit Publish when it's ready.",
            ephemeral=True,
        )

    # --- editing / regenerating the draft ---------------------------------

    async def _on_note_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can edit this.", ephemeral=True)
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message("Couldn't find this draft's record.", ephemeral=True)
            return
        await interaction.response.send_modal(
            TierRetrospectiveModal(self, interaction.message.id, record.get("note"))
        )

    async def _apply_note(self, interaction: discord.Interaction, last_message_id: int, note):
        record = self.store.get(last_message_id)
        if record is None:
            await interaction.followup.send("Couldn't find this draft's record.", ephemeral=True)
            return

        # The note is embedded as its own block inside raw_blocks at
        # generation time (see _build_all_blocks) - the aggregate itself
        # isn't stored, only the rendered blocks, so editing the note
        # means splicing it directly into the frozen block list rather
        # than re-running the full render.
        blocks = list(record["raw_blocks"])
        # Remove a previous note block (rendered as "*note text*") before
        # adding the new one - identified by its leading/trailing "*"
        # italics markers, which nothing else in this post uses.
        blocks = [b for b in blocks if not (b.startswith("*") and b.endswith("*") and not b.startswith("**"))]
        if note:
            blocks.insert(1, f"*{note}*")  # below the title, above the stats - see _build_all_blocks
        record["raw_blocks"] = blocks
        record["note"] = note

        if record.get("published"):
            # Two separate copies to keep in sync: the actual published
            # post (no buttons/footer - public audience) and the sandbox
            # "published_marker" (Edit Note button only) - each tracked
            # under its own message-ID list (published_message_ids vs
            # page_message_ids), since they live in different channels.
            pub_channel = self.bot.get_channel(record["published_channel_id"])
            if pub_channel is not None:
                pub_views = self._render_pages(blocks, destination="published")
                record["published_message_ids"] = await self._reconcile_pages(
                    pub_channel, record["published_message_ids"], pub_views
                )
            sandbox_channel = self.bot.get_channel(int(self.sandbox_channel_id))
            if sandbox_channel is None:
                await interaction.followup.send("Couldn't find the sandbox channel.", ephemeral=True)
                return
            marker_views = self._render_pages(
                blocks, destination="published_marker",
                published_channel_id=record["published_channel_id"], published_by=record.get("published_by"),
            )
            new_ids = await self._reconcile_pages(sandbox_channel, record["page_message_ids"], marker_views)
        else:
            sandbox_channel = self.bot.get_channel(int(self.sandbox_channel_id))
            if sandbox_channel is None:
                await interaction.followup.send("Couldn't find the sandbox channel.", ephemeral=True)
                return
            draft_views = self._render_pages(blocks, destination="draft")
            new_ids = await self._reconcile_pages(sandbox_channel, record["page_message_ids"], draft_views)

        if new_ids[-1] != last_message_id:
            self.store.delete(last_message_id)
        record["page_message_ids"] = new_ids
        self.store.set(new_ids[-1], **record)

        await interaction.followup.send("Updated.", ephemeral=True)

    async def _on_regenerate_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can regenerate this.", ephemeral=True)
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message("Couldn't find this draft's record.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        raid_cog = self.bot.get_cog("RaidSummaryCog")
        if raid_cog is None:
            await interaction.followup.send("RaidSummaryCog isn't loaded.", ephemeral=True)
            return

        tier_data = (
            config.CURRENT_TIER if record["tier_name"] == config.CURRENT_TIER["name"] else config.PREVIOUS_TIER
        )
        agg = await self._aggregate(tier_data, raid_cog)
        if agg is None:
            await interaction.followup.send("No main-raid reports are on record for this tier anymore.", ephemeral=True)
            return

        raw_blocks = self._build_all_blocks(agg, interaction.guild, note=record.get("note"))
        record["raw_blocks"] = raw_blocks
        last_message_id = interaction.message.id

        # Also works on an already-published recap (e.g. re-running after a
        # stat-calculation fix, like resolving alt characters to their
        # main for attendance) - re-syncs BOTH copies the same way
        # _apply_note does: the actual public post (no buttons - see
        # "published" destination) AND the sandbox "published_marker" this
        # button was clicked on, each tracked under its own message-ID
        # list since they live in different channels.
        if record.get("published"):
            pub_channel = self.bot.get_channel(record["published_channel_id"])
            if pub_channel is not None:
                pub_views = self._render_pages(raw_blocks, destination="published")
                record["published_message_ids"] = await self._reconcile_pages(
                    pub_channel, record["published_message_ids"], pub_views
                )
            sandbox_channel = self.bot.get_channel(int(self.sandbox_channel_id))
            if sandbox_channel is None:
                await interaction.followup.send("Couldn't find the sandbox channel.", ephemeral=True)
                return
            marker_views = self._render_pages(
                raw_blocks, destination="published_marker",
                published_channel_id=record["published_channel_id"], published_by=record.get("published_by"),
            )
            new_ids = await self._reconcile_pages(sandbox_channel, record["page_message_ids"], marker_views)
        else:
            sandbox_channel = self.bot.get_channel(int(self.sandbox_channel_id))
            page_views = self._render_pages(raw_blocks, destination="draft")
            new_ids = await self._reconcile_pages(sandbox_channel, record["page_message_ids"], page_views)

        if new_ids[-1] != last_message_id:
            self.store.delete(last_message_id)
        record["page_message_ids"] = new_ids
        self.store.set(new_ids[-1], **record)

        message = (
            "Regenerated with the latest data - the published post has been updated too."
            if record.get("published") else "Regenerated with the latest data."
        )
        await interaction.followup.send(message, ephemeral=True)

    # --- publishing --------------------------------------------------------

    async def _on_publish_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can publish this.", ephemeral=True)
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message("Couldn't find this draft's record.", ephemeral=True)
            return
        if record.get("published"):
            await interaction.response.send_message("This has already been published.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel_select = discord.ui.ChannelSelect(
            placeholder="Choose a channel to publish this retrospective to",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )
        picked = asyncio.Event()
        chosen = {}

        async def _on_select(select_interaction: discord.Interaction):
            if select_interaction.user.id != interaction.user.id:
                await select_interaction.response.send_message("This isn't your draft to publish.", ephemeral=True)
                return
            chosen["channel_id"] = channel_select.values[0].id
            await select_interaction.response.defer()
            picked.set()

        channel_select.callback = _on_select
        view = discord.ui.View(timeout=300)
        view.add_item(channel_select)
        await interaction.followup.send("Publish to which channel?", view=view, ephemeral=True)

        try:
            await asyncio.wait_for(picked.wait(), timeout=DRAFT_PUBLISH_WAIT_SECONDS)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out selecting a channel - click Publish again.", ephemeral=True)
            return

        target_channel = self.bot.get_channel(chosen["channel_id"])
        if target_channel is None:
            await interaction.followup.send("Couldn't find that channel.", ephemeral=True)
            return

        published_by = str(interaction.user)
        page_views = self._render_pages(record["raw_blocks"], destination="published")
        published_messages = [await target_channel.send(view=view) for view in page_views]

        draft_message_id = interaction.message.id
        record["published"] = True
        record["published_channel_id"] = chosen["channel_id"]
        record["published_message_ids"] = [m.id for m in published_messages]
        record["published_by"] = published_by
        self.store.set(draft_message_id, **record)

        # Re-render the sandbox draft in place with the "published" footer
        # and its Edit-note button now retargeted (see _apply_note, which
        # already looks at record["published"] to know where to write).
        sandbox_channel = self.bot.get_channel(int(self.sandbox_channel_id))
        if sandbox_channel is not None:
            marker_views = self._render_pages(
                record["raw_blocks"], destination="published_marker",
                published_channel_id=chosen["channel_id"], published_by=published_by,
            )
            new_draft_ids = await self._reconcile_pages(sandbox_channel, record["page_message_ids"], marker_views)
            if new_draft_ids[-1] != draft_message_id:
                self.store.delete(draft_message_id)
            record["page_message_ids"] = new_draft_ids
            self.store.set(new_draft_ids[-1], **record)

        await interaction.followup.send(f"🚀 Published to <#{chosen['channel_id']}>.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TierRetrospectiveCog(bot))
