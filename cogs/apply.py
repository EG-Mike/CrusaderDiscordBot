"""
Guild application feature (the gear check).

Flow:
  1. /apply character:<name and surname> (or the Start button) - the bot
     reads the character from Blizzard's API (gear.py): class, level,
     guild, talents, and the equipped gear with enchants and gems.
  2. The applicant confirms their main role and spec (the spec with the
     most talent points is suggested) and any other specs they play.
  3. An optional note and screenshots, collected by DM.
  4. The application is posted to the review channel: the gear summary,
     requirements (Settings: minimum level and item level), and, once
     WarcraftLogs covers the game (APPLICATION_WARCRAFTLOGS_ENABLED), the
     applicant's parses. "Refresh gear" re-reads the gear from Blizzard.
  5. An approver approves (roles, nickname, DM, and the character is linked
     to the applicant on the guild roster) or denies (DM).

Moderators can also gear-check any character without an application:
/gearcheck character.
"""

import os
import asyncio
import logging
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import bot_messages
import config
import gear
import icons
import roster
import wow_api
from permissions import is_moderator

log = logging.getLogger("wow-apply-bot.apply")

APPROVE_EMOJI = "✅"
DENY_EMOJI = "❌"
FRESH_ROLE_EMOJI = "<:fresh_role:1542590697238962226>"


def _fresh_label(name: str) -> str:
    """Prefixes the Fresh role's emoji in front of its name wherever it's
    mentioned in an assigned/already-had role list - other exempt roles
    (Regular/Organizer) are left as plain text."""
    return f"{FRESH_ROLE_EMOJI} {name}" if name == "Fresh" else name

DIALOG_TIMEOUT = 300       # role/spec selection
SCREENSHOT_TIMEOUT = 180   # waiting for an optional screenshot reply
NOT_FOUND_TIMEOUT = 180    # waiting for the retry-name/continue-anyway choice

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

ARCHIVE_CONFIRMATION_KEY = "archive_confirmation"  # sentinel store key, not a real message id
GEAR_TEXT_LIMIT = 1800  # the gear list's share of a review card's 4000 characters


def _clip(text: str, limit: int) -> str:
    """Cuts at a line break so an item line isn't split."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 40)
    return text[:cut if cut > 0 else limit - 40] + "\n-# … more items not shown"


def _emoji_for(icon_str):
    """Converts a resolved icon string (e.g. '<:name:123>') into a
    PartialEmoji usable in SelectOption(emoji=...), or None if unavailable."""
    if not icon_str:
        return None
    try:
        return discord.PartialEmoji.from_str(icon_str)
    except Exception:
        return None


def _match_known_spec(specs, wcl_spec_name):
    """
    Matches WarcraftLogs' reported spec name against the class's spec names,
    ignoring spacing/casing differences (e.g. "BeastMastery" vs "Beast
    Mastery") - returns our display name, or None if there's no confident
    match rather than guessing.
    """
    if not wcl_spec_name:
        return None
    normalized = wcl_spec_name.replace(" ", "").lower()
    for s in specs:
        if s.replace(" ", "").lower() == normalized:
            return s
    return None


class RoleSpecView(discord.ui.View):
    """View letting the applicant pick their main role, main spec, and any
    other specs they can also play. 'Continue' moves on to the note/screenshot
    step. Works identically whether sent to a DM channel or a guild channel.
    `specs`: the class's spec names for the game version (Blizzard's)."""

    def __init__(self, user_id, guild, class_name, specs, suggested_role=None, suggested_spec=None):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.user_id = user_id
        self.guild = guild
        self.class_name = class_name
        # Pre-filled from WCL data if available - the applicant can still
        # change either before continuing.
        self.role = suggested_role
        self.main_spec = suggested_spec
        self.other_specs = []

        self.done = asyncio.Event()
        self.specs = specs

        role_options = config.CLASS_ROLES.get(class_name, config.ROLES)
        self.role_select = discord.ui.Select(
            placeholder="Main role" + (f" (suggested: {suggested_role})" if suggested_role else ""),
            options=[
                discord.SelectOption(
                    label=r,
                    emoji=_emoji_for(icons.resolve_role_icon(guild, r)),
                    default=(r == suggested_role),
                )
                for r in role_options
            ],
        )
        self.role_select.callback = self._on_role
        self.add_item(self.role_select)

        self.spec_select = discord.ui.Select(
            placeholder="Main spec" + (f" (suggested: {suggested_spec})" if suggested_spec else ""),
            options=[
                discord.SelectOption(
                    label=s,
                    emoji=_emoji_for(icons.resolve_spec_icon(class_name, s)),
                    default=(s == suggested_spec),
                )
                for s in specs
            ],
        )
        self.spec_select.callback = self._on_spec
        self.add_item(self.spec_select)

        self.other_spec_select = None
        if len(specs) > 1:
            self.other_spec_select = discord.ui.Select(
                placeholder="Other specs you can also play (this is optional)",
                min_values=0,
                max_values=1,  # corrected immediately by _sync_other_spec_options()
                options=[discord.SelectOption(label=specs[0])],  # placeholder, replaced below
            )
            self.other_spec_select.callback = self._on_other_specs
            self._sync_other_spec_options()
            self.add_item(self.other_spec_select)

        continue_button = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary)
        continue_button.callback = self._on_continue
        self.add_item(continue_button)

    def _sync_other_spec_options(self):
        """Rebuilds the 'other specs' options to exclude whatever is
        currently the main spec (no point letting someone tick the same
        spec as both their main and an 'other' choice)."""
        if self.other_spec_select is None:
            return
        specs = self.specs
        available = [s for s in specs if s != self.main_spec] or specs
        # Drop the main spec from any already-chosen "other specs" too, in
        # case it just became the main spec.
        self.other_specs = [s for s in self.other_specs if s != self.main_spec]
        self.other_spec_select.options = [
            discord.SelectOption(
                label=s,
                emoji=_emoji_for(icons.resolve_spec_icon(self.class_name, s)),
                default=(s in self.other_specs),
            )
            for s in available
        ]
        self.other_spec_select.max_values = len(available)

    def _sync_selected_defaults(self):
        """Keeps the role/spec dropdowns showing the currently-selected
        value after a full view refresh (edit_message resends every
        component fresh, so stale `default` flags would visually reset
        selections that were made earlier)."""
        for opt in self.role_select.options:
            opt.default = opt.label == self.role
        for opt in self.spec_select.options:
            opt.default = opt.label == self.main_spec

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your application.", ephemeral=True)
            return False
        return True

    async def _on_role(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.role = self.role_select.values[0]
        await interaction.response.defer()

    async def _on_spec(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.main_spec = self.spec_select.values[0]
        self._sync_selected_defaults()
        self._sync_other_spec_options()
        await interaction.response.edit_message(view=self)

    async def _on_other_specs(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.other_specs = list(self.other_spec_select.values)
        await interaction.response.defer()

    async def _on_continue(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        if not self.role or not self.main_spec:
            await interaction.response.send_message(
                "Please select both a role and a main spec first.", ephemeral=True
            )
            return
        await interaction.response.defer()
        self.done.set()
        self.stop()


class SkipNoteScreenshotView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=SCREENSHOT_TIMEOUT)
        self.user_id = user_id
        self.skipped = asyncio.Event()

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your application.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Skipped - no note or screenshot was attached.\n\n"
            "Your application has been posted for review. You'll get a DM once a reviewer makes a decision.\n\n"
            "**At any time you may use `/application_update` in this chat to provide a note and/or screenshot(s)**", 
            ephemeral=True)
        self.skipped.set()
        self.stop()


class CharacterNameModal(discord.ui.Modal, title="Apply to the guild"):
    character_name = discord.ui.TextInput(
        label="Your character's name and surname",
        placeholder="e.g. Eggmike Thewhite",
        required=True,
        max_length=48,
    )

    def __init__(self, cog: "ApplyCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog._run_application_flow(interaction, str(self.character_name).strip())


class StartApplicationView(discord.ui.View):
    """
    Persistent view for the #gear-check landing message. The button click
    itself only needs View Channel (unlike typing a slash command, which
    Discord requires Send Messages for) - so this works even in a channel
    where @everyone can't post, keeping it clutter-free.
    """

    def __init__(self, cog: "ApplyCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Start Gear-Check procedure",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="wow_apply_start",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CharacterNameModal(self.cog))


class NotFoundView(discord.ui.View):
    """Shown when a character isn't found on Blizzard - lets the applicant either
    correct a typo or confirm the name is right and continue with a
    manual-review application."""

    def __init__(self, cog: "ApplyCog", user_id, character_name):
        super().__init__(timeout=NOT_FOUND_TIMEOUT)
        self.cog = cog
        self.user_id = user_id
        self.character_name = character_name
        self.resolved = asyncio.Event()
        self.action = None  # "retry" or "continue"

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your application.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Re-enter character name", style=discord.ButtonStyle.primary, emoji="✏️")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(CharacterNameModal(self.cog))
        self.action = "retry"
        self.resolved.set()
        self.stop()

    @discord.ui.button(label="That's correct - continue anyway", style=discord.ButtonStyle.secondary, emoji="✅")
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        self.action = "continue"
        self.resolved.set()
        self.stop()


class ClassSelectView(discord.ui.View):
    """Shown when continuing without Blizzard data - there's no class to
    infer from, so the applicant picks it manually before the (otherwise
    identical) role/spec dialog can build its spec dropdown."""

    def __init__(self, user_id, guild):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.user_id = user_id
        self.class_name = None
        self.done = asyncio.Event()

        select = discord.ui.Select(
            placeholder="What class is your character?",
            options=[
                discord.SelectOption(label=c, emoji=_emoji_for(icons.resolve_class_icon(guild, c)))
                for c in config.CLASS_SPECS.keys()
            ],
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your application.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.class_name = self._select.values[0]
        await interaction.response.defer()
        self.done.set()
        self.stop()


class DenyReasonModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(
        label="Reason (optional, mods only)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, cog: "ApplyCog", message_id: int):
        super().__init__()
        self.cog = cog
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog._apply_denial(interaction, self.message_id, str(self.reason).strip() or None)


class ApplyCog(commands.Cog):
    gearcheck_group = app_commands.Group(
        name="gearcheck", description="Gear-check application management (moderator only)"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.review_channel_id = config.REVIEW_CHANNEL_ID
        self.fresh_role_id = config.FRESH_ROLE_ID
        self.mod_role_id = config.MOD_ROLE_ID
        self.server_slug = config.SERVER_SLUG
        self.server_region = config.SERVER_REGION
        self.start_view = StartApplicationView(self)

    async def cog_load(self):
        # Registers the persistent buttons so they keep working across bot
        # restarts, on every previously-posted message.
        self.bot.add_view(self.start_view)
        template = self._render_application_view({
            "applicant_id": 0,
            "character_name": "template",
            "character_slug": "template",
            "status": "pending",
        })
        self.bot.add_view(template)
        bot_messages.register_updater("gear_check_explainer", self._sync_gear_check_explainer)
        bot_messages.register_updater("review_explainer", self._sync_review_explainer)

    # --- embed/layout building -----------------------------------------

    def _format_boss_line(self, name, kills, pct) -> str:
        if kills == 0:
            return f"**{name}** — No logs"
        pct_str = f"{pct:.0f}%" if pct is not None else "?"
        return f"`{pct_str}` **{name}** — `{kills} kills`"

    def _build_boss_list(self, tier_data, bosses: dict) -> str:
        by_encounter_id = {}
        if tier_data:
            for b in tier_data.get("per_boss") or []:
                if b.get("encounter_id") is not None:
                    by_encounter_id[b["encounter_id"]] = b

        lines = []
        for boss_name, encounter_id in bosses.items():
            entry = by_encounter_id.get(encounter_id)
            kills = entry["kills"] if entry else 0
            pct = entry["best_percent"] if entry else None
            lines.append(self._format_boss_line(boss_name, kills, pct))

        value = "\n".join(lines)
        if len(value) > 1000:
            value = value[:1000] + "\n… (truncated, see full profile)"
        return value

    def _build_class_block(self, guild, class_name, spec, role, level, guild_name) -> str:
        class_icon = icons.resolve_class_icon(guild, class_name)
        lines = [f"**Class:** {class_icon} {class_name}".strip()]
        if spec:
            spec_icon = icons.resolve_spec_icon(class_name, spec)
            lines.append(f"**Spec:** {spec_icon} {spec}".strip())
        if role:
            role_icon = icons.resolve_role_icon(guild, role)
            lines.append(f"**Role:** {role_icon} {role}".strip())
        lines.append(f"**Level:** {level if level is not None else '? (not found on Blizzard)'}")
        lines.append(f"**Guild:** {guild_name or 'None'}")
        return "\n".join(lines)

    def _build_also_play_block(self, class_name, other_specs) -> str:
        if not other_specs:
            return "-"
        parts = []
        for s in other_specs:
            icon = icons.resolve_spec_icon(class_name, s)
            parts.append(f"{icon} {s}".strip())
        return ", ".join(parts)

    def _count_full_clears(self, tier_data) -> int:
        """
        Number of full clears of the current tier: the minimum kill count
        across ALL current-tier bosses (a boss with zero logged kills -
        including one entirely absent from the WCL response - caps this at
        0). Deliberately NOT total_kills (a sum across bosses): farming one
        early boss repeatedly would inflate that sum past
        NEW_TIER_LOG_THRESHOLD despite the applicant never having killed the
        rest of the tier.
        """
        kills_by_encounter = {}
        if tier_data:
            for b in tier_data.get("per_boss") or []:
                if b.get("encounter_id") is not None:
                    kills_by_encounter[b["encounter_id"]] = b.get("kills", 0)
        return min(
            (kills_by_encounter.get(eid, 0) for eid in config.CURRENT_TIER["bosses"].values()),
            default=0,
        )

    async def _compute_tier_and_boss_blocks(self, character: dict):
        """
        Live WCL lookups happen ONLY here, once, at posting time. Everything
        rendered afterward (approve/deny/reset/update) reuses the resulting
        text - never re-queries WCL, so a decision doesn't wait on the API
        and a rebuild can't silently change the numbers shown.
        """
        current_tier_data = None
        if character.get("id") and config.CURRENT_TIER.get("zone_id"):
            try:
                current_tier_data = await self.bot.wcl.get_tier_data(
                    character["id"], config.CURRENT_TIER["zone_id"]
                )
            except Exception:
                log.exception("Failed to fetch current-tier data")

        full_clears = self._count_full_clears(current_tier_data)
        current_has_data = bool(
            current_tier_data and current_tier_data.get("best_performance_average") is not None
        )
        # Fewer than NEW_TIER_LOG_THRESHOLD full clears - including zero, i.e. no
        # current-tier data at all - is what triggers the previous-tier fallback,
        # not whether any current-tier data exists at all.
        low_kill_count = full_clears < config.NEW_TIER_LOG_THRESHOLD

        prev_tier_data = None
        if (
            low_kill_count
            and character.get("id")
            and config.PREVIOUS_TIER
            and config.PREVIOUS_TIER.get("zone_id")
        ):
            try:
                prev_tier_data = await self.bot.wcl.get_tier_data(
                    character["id"], config.PREVIOUS_TIER["zone_id"]
                )
            except Exception:
                log.exception("Failed to fetch previous-tier fallback data")
                prev_tier_data = None
            if prev_tier_data and prev_tier_data.get("best_performance_average") is None:
                prev_tier_data = None

        tier_lines = []
        if low_kill_count:
            clear_word = "clear" if full_clears == 1 else "clears"
            note = (
                f", so {config.PREVIOUS_TIER['name']} data is also included below."
                if prev_tier_data else "."
            )
            tier_lines.append(
                f"⚠️ **Limited current-tier clears** - this character has only {full_clears} "
                f"full {clear_word} of {config.CURRENT_TIER['name']}{note}"
            )
        if current_has_data:
            tier_lines.append(
                f"**Best Avg ({config.CURRENT_TIER['name']}):** "
                f"{current_tier_data['best_performance_average']:.1f}%"
            )
            tier_lines.append(
                f"**Median Avg ({config.CURRENT_TIER['name']}):** "
                f"{current_tier_data['median_performance_average']:.1f}%"
            )
        if prev_tier_data:
            tier_lines.append(
                f"**Best Avg ({config.PREVIOUS_TIER['name']}):** "
                f"{prev_tier_data['best_performance_average']:.1f}%"
            )
            tier_lines.append(
                f"**Median Avg ({config.PREVIOUS_TIER['name']}):** "
                f"{prev_tier_data['median_performance_average']:.1f}%"
            )

        tier_block = "\n".join(tier_lines) if tier_lines else None
        boss_block = (
            f"**Per-boss best parse & kills ({config.CURRENT_TIER['name']})**\n"
            + self._build_boss_list(current_tier_data, config.CURRENT_TIER["bosses"])
        )
        if prev_tier_data:
            boss_block += (
                f"\n\n**Per-boss best parse & kills ({config.PREVIOUS_TIER['name']})**\n"
                + self._build_boss_list(prev_tier_data, config.PREVIOUS_TIER["bosses"])
            )
        hidden_block = None
        if character.get("hidden"):
            hidden_block = "⚠️ **Note:** This character's logs are marked hidden/private on WarcraftLogs."

        return tier_block, boss_block, hidden_block

    def _render_application_view(self, application: dict, message_id: int = None) -> discord.ui.LayoutView:
        """
        Rebuilds the whole application layout from stored data - always
        from scratch, never by mutating an existing message's components.
        Used both for the initial post AND every subsequent edit (approve,
        deny, reset, /update-application), so there's exactly one code path
        that knows how to render an application, and no dependency on
        component-mutation APIs.

        message_id, if given, is this application's OWN message id - used
        to exclude itself when counting prior denials (without this, an
        application that was just denied would count itself as a "prior"
        denial the moment it's rendered post-decision).
        """
        view = discord.ui.LayoutView(timeout=None)
        color = discord.Color(application.get("class_color") or 0x2B2D31)
        container = discord.ui.Container(accent_color=color)

        status = application.get("status", "pending")
        is_pending = status == "pending"
        icon_prefix = {"approved": f"{APPROVE_EMOJI} ", "denied": f"{DENY_EMOJI} "}.get(status, "")
        suffix = {"approved": " (APPROVED)", "denied": " (DENIED)"}.get(status, "")
        profile_url = application.get("profile_url") or ""
        character_name = application.get("character_name", "?")
        if profile_url:
            title = f"## {icon_prefix}[{character_name}{suffix}]({profile_url})"
        else:
            title = f"## {icon_prefix}{character_name}{suffix}"
        container.add_item(discord.ui.TextDisplay(title))
        container.add_item(discord.ui.TextDisplay(f"Applicant: <@{application['applicant_id']}>"))
        container.add_item(discord.ui.Separator())

        prior_denials = [
            (mid, rec)
            for mid, rec in self.bot.store.find_all_by_applicant(application["applicant_id"])
            if rec.get("status") == "denied" and mid != message_id
        ]
        # Only worth flagging once it's a pattern, not a single past denial.
        if len(prior_denials) >= 2:
            most_recent_id, most_recent_rec = prior_denials[-1]
            most_recent_date = discord.utils.snowflake_time(most_recent_id).strftime("%Y-%m-%d")
            container.add_item(discord.ui.TextDisplay(
                f"⚠️ **Previously Denied** ⚠️\nThis character has been denied {len(prior_denials)} "
                f"times before. Most recent: **{most_recent_rec.get('character_name', '?')}** "
                f"on {most_recent_date}."
            ))
            container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(application.get("class_block") or "-"))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            f"**Can also play:** {application.get('also_play_block') or '-'}"
        ))
        container.add_item(discord.ui.Separator())

        gear_text = application.get("gear_text")
        if gear_text:
            checked = application.get("gear_checked_at")
            when = f" · checked <t:{checked}:R>" if checked else ""
            container.add_item(discord.ui.TextDisplay(
                f"### Gear\n-# From Blizzard, as of the character's last logout{when}\n{gear_text['overview']}"))
            container.add_item(discord.ui.TextDisplay(_clip(gear_text["gear"], GEAR_TEXT_LIMIT)))
            if gear_text.get("flags"):
                container.add_item(discord.ui.TextDisplay(gear_text["flags"]))
            container.add_item(discord.ui.Separator())
        elif application.get("armory_block"):  # applications from before the gear check
            container.add_item(discord.ui.TextDisplay(application["armory_block"]))
            container.add_item(discord.ui.Separator())

        if application.get("tier_block"):
            container.add_item(discord.ui.TextDisplay(application["tier_block"]))
            container.add_item(discord.ui.Separator())

        if application.get("boss_block"):
            container.add_item(discord.ui.TextDisplay(application["boss_block"]))
            container.add_item(discord.ui.Separator())

        if application.get("hidden_block"):
            container.add_item(discord.ui.TextDisplay(application["hidden_block"]))
            container.add_item(discord.ui.Separator())

        links = [f"[Profile]({profile_url})" if profile_url else None,
                 f"[WarcraftLogs]({application['wcl_url']})" if application.get("wcl_url") else None]
        if any(links):
            container.add_item(discord.ui.TextDisplay("**Links:** " + " · ".join(filter(None, links))))
            container.add_item(discord.ui.Separator())

        note = application.get("note")
        container.add_item(discord.ui.TextDisplay(
            f"**Applicant's note:**\n{note[:1024] if note else 'No note provided'}"
        ))
        container.add_item(discord.ui.Separator())

        screenshot_urls = (application.get("screenshot_urls") or [])[: config.MAX_SCREENSHOTS]
        if screenshot_urls:
            gallery = discord.ui.MediaGallery(
                *[discord.MediaGalleryItem(u) for u in screenshot_urls]
            )
            container.add_item(gallery)
        else:
            container.add_item(discord.ui.TextDisplay("**Screenshot(s):** No screenshot provided"))
        container.add_item(discord.ui.Separator())

        footer_text = application.get("decision_footer") or "Use the buttons below to decide"
        container.add_item(discord.ui.TextDisplay(footer_text))

        view.add_item(container)

        action_row = discord.ui.ActionRow()

        approve_button = discord.ui.Button(
            label="Approve",
            emoji=APPROVE_EMOJI,
            style=discord.ButtonStyle.success,
            custom_id="wow_apply_approve",
            disabled=not is_pending,
        )
        approve_button.callback = self._on_approve_click
        action_row.add_item(approve_button)

        deny_button = discord.ui.Button(
            label="Deny",
            emoji=DENY_EMOJI,
            style=discord.ButtonStyle.danger,
            custom_id="wow_apply_deny",
            disabled=not is_pending,
        )
        deny_button.callback = self._on_deny_click
        action_row.add_item(deny_button)

        reset_button = discord.ui.Button(
            label="Reset",
            emoji="🔄",
            style=discord.ButtonStyle.primary,
            custom_id="wow_apply_reset",
            disabled=is_pending,
        )
        reset_button.callback = self._on_reset_click
        action_row.add_item(reset_button)

        refresh_button = discord.ui.Button(
            label="Refresh gear",
            emoji="🛡️",
            style=discord.ButtonStyle.secondary,
            custom_id="wow_apply_refresh_gear",
            disabled=not application.get("character_slug"),
        )
        refresh_button.callback = self._on_refresh_gear_click
        action_row.add_item(refresh_button)

        view.add_item(action_row)

        return view

    async def _refresh_review_message(self, message: discord.Message, application: dict):
        await message.edit(view=self._render_application_view(application, message_id=message.id))

    async def _gear_fields(self, summary) -> dict:
        """What an application stores about the gear: the summary (data) and
        its rendered text, so a re-render never needs Blizzard."""
        if not summary:
            return {"gear": None, "gear_text": None, "gear_checked_at": None}
        icons_by_item = await gear.item_icons(self.bot, summary)
        return {"gear": summary, "gear_text": gear.render_lines(summary, icons_by_item),
                "gear_checked_at": int(discord.utils.utcnow().timestamp())}

    async def _post_application(
        self, interaction, char_data, role, spec, other_specs, note, screenshot_urls
    ):
        class_block = self._build_class_block(
            interaction.guild, char_data["class_name"], spec, role,
            char_data.get("level"), char_data.get("guild_name"),
        )
        also_play_block = self._build_also_play_block(char_data["class_name"], other_specs)
        tier_block = boss_block = hidden_block = None
        if config.APPLICATION_WARCRAFTLOGS_ENABLED and char_data.get("id"):
            tier_block, boss_block, hidden_block = await self._compute_tier_and_boss_blocks(char_data)

        application = {
            "applicant_id": interaction.user.id,
            "character_name": char_data["name"],
            "character_slug": char_data.get("slug"),
            "status": "pending",
            "class_name": char_data["class_name"],
            "role": role,
            "spec": spec,
            "other_specs": other_specs,
            "note": note,
            "class_color": config.CLASS_COLORS.get(char_data["class_name"], 0x2B2D31),
            "last_decision": None,
            "class_block": class_block,
            "also_play_block": also_play_block,
            "tier_block": tier_block,
            "boss_block": boss_block,
            "hidden_block": hidden_block,
            "profile_url": char_data.get("profile_url"),
            "wcl_url": char_data.get("wcl_url"),
            "screenshot_urls": screenshot_urls or [],
            **await self._gear_fields(char_data.get("gear")),
        }

        review_channel = self.bot.get_channel(self.review_channel_id)
        if review_channel is None:
            log.error("Can't post %s's application: REVIEW_CHANNEL_ID isn't set to a channel the bot can see",
                      application["character_name"])
            await interaction.followup.send(
                "Sorry, applications can't be submitted right now (the review channel isn't set up). "
                "Please let a moderator know.",
                ephemeral=True,
            )
            return
        view = self._render_application_view(application)
        message = await review_channel.send(view=view)

        fields = {k: v for k, v in application.items() if k not in ("applicant_id", "character_name")}
        self.bot.store.add(message.id, application["applicant_id"], application["character_name"], **fields)

        try:
            await interaction.user.send(
                f"✅ Your application for **{application['character_name']}** has been "
                "posted for review. You'll get a DM once a reviewer makes a decision."
            )
        except discord.Forbidden:
            pass  # DMs closed - the ephemeral confirmation below still covers this

        await interaction.followup.send(
            "Your application has been posted for review. You'll get a DM once "
            "a reviewer makes a decision.",
            ephemeral=True,
        )

    # --- shared helpers -----------------------------------------------

    async def _try_open_dm(self, user: discord.User):
        try:
            return await user.create_dm()
        except discord.Forbidden:
            return None

    async def _suggest_spec_role(self, char_data, specs):
        """The spec with the most talent points (Blizzard); with WarcraftLogs
        parses on and no talents spent, the spec logged most this tier."""
        suggested_spec = (char_data.get("gear") or {}).get("suggested_spec")
        if suggested_spec not in specs:
            suggested_spec = None
        if not suggested_spec and config.APPLICATION_WARCRAFTLOGS_ENABLED and char_data.get("id"):
            for tier in (config.CURRENT_TIER, config.PREVIOUS_TIER):
                if suggested_spec or not tier or not tier.get("zone_id"):
                    continue
                try:
                    tier_data = await self.bot.wcl.get_tier_data(char_data["id"], tier["zone_id"])
                except Exception:
                    log.exception("Failed to fetch tier data for the spec suggestion")
                    continue
                if tier_data:
                    suggested_spec = _match_known_spec(specs, tier_data.get("primary_spec"))

        suggested_role = config.SPEC_DEFAULT_ROLE.get(suggested_spec) if suggested_spec else None
        if suggested_role not in config.CLASS_ROLES.get(char_data["class_name"], config.ROLES):
            suggested_role = None
        return suggested_spec, suggested_role

    async def _collect_note_screenshot_and_post(
        self, interaction, char_data, role, spec, other_specs, dm_channel
    ):
        """
        Collects an optional note+screenshot(s) via DM, then posts the
        application. If dm_channel is None (DMs closed), posts immediately
        without collecting them - the applicant can still add one later
        with /update-application.
        """
        if dm_channel is None:
            await interaction.followup.send(
                "I couldn't DM you to collect an optional note/screenshot - your "
                "DMs might be closed to server members. Posting your application "
                "without them for now; you can add a screenshot later with "
                "/update-application once you enable DMs.",
                ephemeral=True,
            )
            await self._post_application(interaction, char_data, role, spec, other_specs, None, [])
            return

        skip_view = SkipNoteScreenshotView(interaction.user.id)
        await dm_channel.send(
            bot_messages.text("note_prompt_dm"),
            view=skip_view,
        )

        await interaction.followup.send(
            f"\nThank you, **{char_data['name']}**, please check your DMs to continue the process",
            ephemeral=True,
        )

        def _is_applicant_dm_reply(m: discord.Message) -> bool:
            return m.author.id == interaction.user.id and m.channel.id == dm_channel.id

        message_task = asyncio.create_task(
            self.bot.wait_for("message", check=_is_applicant_dm_reply, timeout=SCREENSHOT_TIMEOUT)
        )
        skip_task = asyncio.create_task(skip_view.skipped.wait())
        done, pending = await asyncio.wait(
            {message_task, skip_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

        note_text = None
        screenshot_urls = []
        timed_out = False
        if message_task in done:
            try:
                msg = message_task.result()
                note_text = msg.content.strip() or None
                screenshot_urls = [
                    a.url for a in msg.attachments if (a.content_type or "").startswith("image/")
                ]
            except asyncio.TimeoutError:
                timed_out = True
            except asyncio.CancelledError:
                pass

        if timed_out:
            try:
                await dm_channel.send(bot_messages.text("note_timeout_dm"))
            except discord.Forbidden:
                pass

        await self._post_application(
            interaction, char_data, role, spec, other_specs, note_text, screenshot_urls
        )

    # --- /apply command and the button+modal entry point ------------------
    # Both funnel into _run_application_flow after their own initial
    # response - the slash command defers immediately, the modal defers in
    # its on_submit. From that point on the flow is identical.

    @app_commands.command(name="apply", description="Apply to the guild with your character name")
    @app_commands.describe(character="Your character's exact in-game name")
    async def apply(self, interaction: discord.Interaction, character: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._run_application_flow(interaction, character.strip())

    async def _class_names(self) -> dict:
        """{class id: name} and {class name: [spec names]} for the game version, from Blizzard."""
        try:
            ref = await wow_api.reference(self.bot)
        except wow_api.WowApiError:
            ref = {"classes": []}
        return ({c["id"]: c["name"] for c in ref.get("classes", [])},
                {c["name"]: [sp["name"] for sp in c["specs"]] for c in ref.get("classes", [])})

    def _profile_url(self, character_slug: str):
        template = (config.GEARCHECK_PROFILE_URL or "").strip()
        if not template or not character_slug:
            return None
        return template.replace("{region}", wow_api.region()).replace("{slug}", character_slug)

    async def _run_application_flow(self, interaction: discord.Interaction, character: str):
        if getattr(self.bot, "blizzard", None) is None:
            await self._handle_not_found(interaction, character)
            return
        class_names, _ = await self._class_names()
        try:
            summary = await gear.fetch(self.bot, wow_api.slug(character), class_names)
        except gear.GearError:
            await self._handle_not_found(interaction, character)
            return
        except wow_api.WowApiError:
            log.exception("Blizzard lookup failed")
            await interaction.followup.send(
                "Something went wrong looking that character up on Blizzard. Please try again in a minute, or let "
                "a moderator know.", ephemeral=True)
            return
        char_data = {
            "id": None,  # WarcraftLogs id, when parses are included
            "name": summary["character"],
            "slug": summary["slug"],
            "class_name": summary["class_name"],
            "level": summary["level"],
            "guild_name": summary["guild"],
            "profile_url": self._profile_url(summary["slug"]),
            "gear": summary,
        }
        await self._add_warcraftlogs_id(char_data)
        await self._continue_with_found_character(interaction, char_data)

    async def _add_warcraftlogs_id(self, char_data: dict):
        """With WarcraftLogs parses on: the character's WarcraftLogs id and
        profile, for the tier and boss blocks. Never fails the application."""
        if not config.APPLICATION_WARCRAFTLOGS_ENABLED or getattr(self.bot, "wcl", None) is None:
            return
        try:
            found = await self.bot.wcl.get_character(char_data["name"], self.server_slug, self.server_region)
        except Exception:
            log.warning("WarcraftLogs lookup failed for %s", char_data["name"], exc_info=True)
            return
        if found:
            char_data["id"] = found.get("id")
            char_data["hidden"] = found.get("hidden")
            char_data["wcl_url"] = found.get("profile_url")

    async def _handle_not_found(self, interaction: discord.Interaction, character: str):
        view = NotFoundView(self, interaction.user.id, character)
        await interaction.followup.send(
            f"I couldn't find **{character}** on Blizzard. Check the spelling: your character's name and "
            f"surname, e.g. Eggmike Thewhite. Characters can be found from level {roster.PROFILE_MIN_LEVEL}, "
            "after logging out once. If the name is right, you can continue anyway and a moderator will review "
            "your application by hand.",
            view=view,
            ephemeral=True,
        )

        try:
            await asyncio.wait_for(view.resolved.wait(), timeout=NOT_FOUND_TIMEOUT + 10)
        except asyncio.TimeoutError:
            return  # view times out silently - they can just run /apply again

        if view.action == "retry":
            # The button already opened a fresh CharacterNameModal - that
            # submission (if any) kicks off its own independent flow. Nothing
            # more to do on this one.
            return

        # action == "continue": no Blizzard data means no known class to build
        # a spec dropdown from - ask for it manually, then everything else
        # (role/spec/note/screenshot) is identical to the found-character
        # flow, just with no pre-filled suggestions.
        class_view = ClassSelectView(interaction.user.id, interaction.guild)
        await interaction.followup.send(
            "Since Blizzard has no data for this character, please select their class:",
            view=class_view,
            ephemeral=True,
        )

        try:
            await asyncio.wait_for(class_view.done.wait(), timeout=DIALOG_TIMEOUT + 10)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Timed out waiting for class selection - please run /apply again.",
                ephemeral=True,
            )
            return

        char_data = {
            "id": None,
            "name": character,
            "slug": wow_api.slug(character),
            "class_name": class_view.class_name,
            "level": None,
            "guild_name": None,
            "profile_url": None,
            "gear": None,
        }
        await self._continue_with_found_character(interaction, char_data)

    async def _continue_with_found_character(self, interaction: discord.Interaction, char_data: dict):
        _, class_specs = await self._class_names()
        specs = class_specs.get(char_data["class_name"]) or config.CLASS_SPECS.get(char_data["class_name"], [])
        suggested_spec, suggested_role = await self._suggest_spec_role(char_data, specs)

        view = RoleSpecView(
            interaction.user.id,
            interaction.guild,
            char_data["class_name"],
            specs,
            suggested_role=suggested_role,
            suggested_spec=suggested_spec,
        )
        suggestion_note = (
            " I've pre-selected the spec your talent points are in - please confirm or change it."
            if suggested_spec
            else ""
        )
        prompt_text = (
            f"Found **{char_data['name']}**! Pick your main role and spec "
            f"(and any other specs you can play):{suggestion_note}"
        )

        # Try to do the whole rest of the flow (role/spec, note, screenshot)
        # in one continuous DM conversation. If DMs are closed, fall back to
        # role/spec here in-channel (ephemeral - still private) and skip
        # note/screenshot, same as if DM failed later in the old flow.
        dm_channel = await self._try_open_dm(interaction.user)

        if dm_channel is not None:
            await dm_channel.send(prompt_text, view=view)
            await interaction.followup.send(
                f"\nThanks! I found **{char_data['name']}** - please check your DMs to continue.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(prompt_text, view=view, ephemeral=True)

        try:
            await asyncio.wait_for(view.done.wait(), timeout=DIALOG_TIMEOUT + 10)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Application timed out waiting for role/spec selection - please click the button again.",
                ephemeral=True,
            )
            return

        await self._collect_note_screenshot_and_post(
            interaction, char_data, view.role, view.main_spec, view.other_specs, dm_channel
        )

    # --- /update-application command --------------------------------------
    # Separate from the review-channel Reset button on purpose: the
    # applicant typically can't see the (mod-only) review channel, so a
    # button there would be unusable for them. This command works from
    # anywhere the bot can see the person - including DMs.

    @app_commands.command(
        name="update-application",
        description="Add or update the note/screenshot(s) on your latest pending application",
    )
    async def update_application(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        result = self.bot.store.find_latest_by_applicant(interaction.user.id)
        if result is None:
            await interaction.followup.send(
                "You don't have an application on file yet - use /apply first.",
                ephemeral=True,
            )
            return
        message_id, application = result

        if application["status"] != "pending":
            await interaction.followup.send(
                f"Your application has already been **{application['status']}** and can no "
                "longer be updated this way. If you'd like to be reconsidered, please reach "
                "out to an officer.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Reply here with an updated note and/or gear screenshot(s) (up to "
            f"{config.MAX_SCREENSHOTS} images) within 3 minutes.",
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=SCREENSHOT_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Timed out waiting for a reply - run /update-application again to retry.",
                ephemeral=True,
            )
            return

        note_text = msg.content.strip() or None
        screenshot_urls = [
            a.url for a in msg.attachments if (a.content_type or "").startswith("image/")
        ]
        if not note_text and not screenshot_urls:
            await interaction.followup.send(
                "I didn't see a note or an image in that message - nothing updated.",
                ephemeral=True,
            )
            return

        # Re-check status - a moderator could have decided while we were
        # waiting for the reply above. Never touch a decided application.
        application = self.bot.store.get(message_id)
        if application is None or application["status"] != "pending":
            await interaction.followup.send(
                "Your application was decided while waiting for your reply, so this "
                "update wasn't applied.",
                ephemeral=True,
            )
            return

        review_channel = self.bot.get_channel(self.review_channel_id)
        try:
            if review_channel is None:
                review_message = None
            else:
                review_message = await review_channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            review_message = None
        if review_message is None:
            await interaction.followup.send(
                "Couldn't find your application's message to update - please "
                "let a moderator know.",
                ephemeral=True,
            )
            return

        if note_text:
            self.bot.store.update(message_id, note=note_text)
        if screenshot_urls:
            self.bot.store.update(message_id, screenshot_urls=screenshot_urls)

        updated = self.bot.store.get(message_id)
        await self._refresh_review_message(review_message, updated)

        updated_parts = []
        if note_text:
            updated_parts.append("note")
        if screenshot_urls:
            updated_parts.append(f"{len(screenshot_urls)} screenshot(s)")
        await interaction.followup.send(f"Updated: {', '.join(updated_parts)}.", ephemeral=True)

    # --- pinned explainers (texts editable on the dashboard) ----------------

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("Apply cog startup: checking gear-check and review-channel explainers...")
        for key, updater in (("gear_check_explainer", self._sync_gear_check_explainer),
                             ("review_explainer", self._sync_review_explainer)):
            try:
                log.info("Apply cog startup: %s - %s", key, await updater())
            except bot_messages.BotMessageError as exc:
                log.warning("Apply cog startup: %s not posted: %s", key, exc)
        log.info("Apply cog startup: complete")

    async def _sync_review_explainer(self) -> str:
        channel = self.bot.get_channel(self.review_channel_id) if self.review_channel_id else None
        return await bot_messages.sync_pinned(channel, "review_explainer")

    async def _sync_gear_check_explainer(self) -> str:
        """The gear-check explainer carries the Start button (self.start_view)."""
        channel_id = config.GEAR_CHECK_CHANNEL_ID or None
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        return await bot_messages.sync_pinned(channel, "gear_check_explainer", view=self.start_view)

    # --- moderator approve/deny/reset --------------------------------------

    async def _is_mod(self, guild, user_id) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    async def _is_approver(self, guild, user_id) -> bool:
        """Gate for the Approve/Deny/Reset buttons specifically - see
        config.APPROVER_ROLE_ID's comment for why this is separate from
        the broader _is_mod check every other action in this cog uses."""
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        return any(role.id == config.APPROVER_ROLE_ID for role in member.roles)

    async def _on_refresh_gear_click(self, interaction: discord.Interaction):
        application = self.bot.store.get(interaction.message.id)
        if application is None or not application.get("character_slug"):
            await interaction.response.send_message("Couldn't find this application's character.", ephemeral=True)
            return
        if not (is_moderator(interaction.user) or await self._is_approver(interaction.guild, interaction.user.id)):
            await interaction.response.send_message("Only moderators and approvers can refresh the gear.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        class_names, _ = await self._class_names()
        try:
            summary = await gear.fetch(self.bot, application["character_slug"], class_names)
        except (gear.GearError, wow_api.WowApiError) as exc:
            await interaction.followup.send(f"Couldn't refresh the gear: {exc}", ephemeral=True)
            return
        self.bot.store.update(interaction.message.id, **await self._gear_fields(summary))
        await self._refresh_review_message(interaction.message, self.bot.store.get(interaction.message.id))
        await interaction.followup.send(
            "Gear refreshed from Blizzard (as of the character's last logout).", ephemeral=True)

    async def _on_reset_click(self, interaction: discord.Interaction):
        application = self.bot.store.get(interaction.message.id)
        if application is None:
            await interaction.response.send_message(
                "Couldn't find this application's record.", ephemeral=True
            )
            return
        if not await self._is_approver(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only approvers can reset an application.", ephemeral=True
            )
            return
        if application["status"] == "pending":
            await interaction.response.send_message(
                "This application is already pending - nothing to reset.", ephemeral=True
            )
            return

        self.bot.store.update(
            interaction.message.id,
            status="pending",
            last_decision=application["status"],
            decision_footer=f"Reset by {interaction.user} - use the buttons to re-decide",
        )

        updated = self.bot.store.get(interaction.message.id)
        await self._refresh_review_message(interaction.message, updated)

        await interaction.response.send_message(
            "Application reset - the Approve/Deny buttons are active again.", ephemeral=True
        )

    async def _grant_role(self, guild, applicant, role_id, label, newly_assigned, already_had, reason):
        """Assigns role_id to applicant if not already present, tracking the
        outcome in the newly_assigned/already_had lists (both hold
        display labels, e.g. an icon + name string)."""
        if role_id is None:
            return
        role = guild.get_role(role_id)
        if role is None:
            return
        if role in applicant.roles:
            already_had.append(label)
            return
        try:
            await applicant.add_roles(role, reason=reason)
            newly_assigned.append(label)
        except discord.Forbidden:
            log.warning("Missing permission to assign role %s to %s", role.name, applicant)

    def _class_or_role_label(self, guild, name: str, *, is_class: bool) -> str:
        icon = (
            icons.resolve_class_icon(guild, name)
            if is_class
            else icons.resolve_role_icon(guild, name)
        )
        return f"{icon} {name}".strip()

    async def _on_approve_click(self, interaction: discord.Interaction):
        application = self.bot.store.get(interaction.message.id)
        if application is None:
            await interaction.response.send_message(
                "Couldn't find this application's record.", ephemeral=True
            )
            return
        if application["status"] != "pending":
            await interaction.response.send_message(
                "This application has already been decided - use Reset first if you need "
                "to change it.",
                ephemeral=True,
            )
            return
        if not await self._is_approver(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only approvers can approve or deny applications.", ephemeral=True
            )
            return

        # Deferred message update - silently acknowledges the click, no
        # visible loading state, and gives us time for role/DM work before
        # editing the message ourselves.
        await interaction.response.defer()
        await self._apply_approval(interaction, interaction.message.id)

    async def _on_deny_click(self, interaction: discord.Interaction):
        application = self.bot.store.get(interaction.message.id)
        if application is None:
            await interaction.response.send_message(
                "Couldn't find this application's record.", ephemeral=True
            )
            return
        if application["status"] != "pending":
            await interaction.response.send_message(
                "This application has already been decided - use Reset first if you need "
                "to change it.",
                ephemeral=True,
            )
            return
        if not await self._is_approver(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only approvers can approve or deny applications.", ephemeral=True
            )
            return

        # A modal must be the FIRST response to an interaction, so this one
        # can't defer first the way approve does - the modal's own
        # on_submit defers instead, then calls _apply_denial.
        await interaction.response.send_modal(DenyReasonModal(self, interaction.message.id))

    async def _apply_approval(self, interaction: discord.Interaction, message_id: int):
        application = self.bot.store.get(message_id)
        if application is None or application["status"] != "pending":
            return  # decided or removed while we were working - nothing to do

        guild = interaction.guild
        channel = interaction.channel
        applicant = guild.get_member(application["applicant_id"])
        reviewer = interaction.user
        character_name = application["character_name"]
        class_name = application.get("class_name")
        role_name = application.get("role")
        previous_decision = application.get("last_decision")

        newly_assigned = []
        already_had = []
        nickname_line = "Name not changed - no applicant member found"

        if applicant is not None:
            applicant_role_ids = {r.id for r in applicant.roles}
            held_exempt_roles = [
                name for rid, name in config.FRESH_EXEMPT_ROLE_IDS.items()
                if rid in applicant_role_ids
            ]
            is_exempt = bool(held_exempt_roles)
            held_exempt_display = "/".join(_fresh_label(name) for name in held_exempt_roles)

            if is_exempt:
                already_had.append(held_exempt_display)
            else:
                await self._grant_role(
                    guild, applicant, self.fresh_role_id, _fresh_label("Fresh"),
                    newly_assigned, already_had, "Guild application approved",
                )

            # Auto class/role tags - independent of the Fresh exemption,
            # a Regular/Organizer should still get tagged for their class/role.
            if role_name and role_name in config.AUTO_ROLE_IDS:
                label = self._class_or_role_label(guild, role_name, is_class=False)
                await self._grant_role(
                    guild, applicant, config.AUTO_ROLE_IDS[role_name], label,
                    newly_assigned, already_had, "Guild application approved",
                )
            if class_name and class_name in config.AUTO_ROLE_IDS:
                label = self._class_or_role_label(guild, class_name, is_class=True)
                await self._grant_role(
                    guild, applicant, config.AUTO_ROLE_IDS[class_name], label,
                    newly_assigned, already_had, "Guild application approved",
                )

            original_name = applicant.display_name
            if is_exempt:
                nickname_line = f"Name not changed - already {held_exempt_display} ({original_name})"
            else:
                try:
                    await applicant.edit(nick=character_name, reason="Guild application approved")
                    nickname_line = f"Name changed: {original_name} → {character_name}"
                except discord.Forbidden:
                    log.warning("Missing permission to set nickname for %s", applicant)
                    nickname_line = f"⚠️ Name NOT changed (missing permission) - still {original_name}"

            # Notify on a first-ever approval, or when correcting a
            # previous denial. Re-approving after an already-approved
            # state (double reset) has nothing new to tell them.
            should_dm = previous_decision is None or previous_decision == "denied"
            if should_dm:
                try:
                    await applicant.send(bot_messages.text("approval_dm", character=application['character_name']))
                except discord.Forbidden:
                    await channel.send(
                        f"(Couldn't DM {applicant.mention} - their DMs may be closed. "
                        "Please let them know they've been accepted.)"
                    )

        roster_line = await self._link_on_roster(application)

        now = discord.utils.utcnow().astimezone(AMSTERDAM_TZ)
        timestamp_str = now.strftime("%Y-%m-%d %H:%M %Z")

        assigned_display = ", ".join(newly_assigned) if newly_assigned else "None"
        already_display = f"\nAlready had: {', '.join(already_had)}" if already_had else ""
        self.bot.store.update(
            message_id,
            status="approved",
            last_decision="approved",
            decision_footer=(
                f"✅ Approved by {reviewer} • {timestamp_str}\n"
                f"Role(s) assigned: {assigned_display}{already_display}\n"
                f"{nickname_line}" + (f"\n{roster_line}" if roster_line else "")
            ),
        )

        updated = self.bot.store.get(message_id)
        await self._refresh_review_message(interaction.message, updated)

    async def _link_on_roster(self, application: dict):
        """Links the approved character to the applicant on the guild roster
        (when the roster is on). A line for the decision footer, or None."""
        if self.bot.get_cog("RosterCog") is None or not application.get("character_slug"):
            return None
        try:
            found = await roster.find_or_add(self.bot, application["character_slug"])
            await asyncio.to_thread(roster.link, found["slug"], application["applicant_id"],
                                    by="application approval", as_moderator=False)
        except roster.RosterError as exc:
            return f"Guild roster: not linked ({exc})"
        except Exception:
            log.exception("Linking %s on the guild roster failed", application["character_slug"])
            return "Guild roster: not linked (see the System log)"
        return "Guild roster: character linked to the applicant"

    async def _apply_denial(self, interaction: discord.Interaction, message_id: int, reason: str):
        application = self.bot.store.get(message_id)
        if application is None or application["status"] != "pending":
            await interaction.followup.send(
                "This application is no longer pending - nothing changed.", ephemeral=True
            )
            return

        guild = interaction.guild
        channel = interaction.channel
        applicant = guild.get_member(application["applicant_id"])
        reviewer = interaction.user
        character_name = application["character_name"]
        previous_decision = application.get("last_decision")

        if applicant is not None:
            role = guild.get_role(self.fresh_role_id)
            if role is not None and role in applicant.roles:
                try:
                    await applicant.remove_roles(role, reason="Guild application denied/corrected")
                except discord.Forbidden:
                    log.warning("Missing permission to remove role from %s", applicant)

            # Only notify on a first-ever denial. If this is a
            # correction of a previous approval, just revoke the role
            # silently - no DM, per the correction rules.
            should_dm = previous_decision is None
            if should_dm:
                try:
                    await applicant.send(bot_messages.text("denial_dm", character=character_name))
                except discord.Forbidden:
                    await channel.send(
                        f"(Couldn't DM {applicant.mention} - their DMs may be closed. "
                        "Please let them know about the decision.)"
                    )

        now = discord.utils.utcnow().astimezone(AMSTERDAM_TZ)
        timestamp_str = now.strftime("%Y-%m-%d %H:%M %Z")

        footer = f"❌ Denied by {reviewer} • {timestamp_str}\nRoles assigned: None"
        if reason:
            footer += f"\nReason: {reason}"

        self.bot.store.update(
            message_id,
            status="denied",
            last_decision="denied",
            decision_footer=footer,
        )

        updated = self.bot.store.get(message_id)
        review_channel = self.bot.get_channel(self.review_channel_id)
        if review_channel is None:
            return
        try:
            message = await review_channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        await self._refresh_review_message(message, updated)

    # --- /gearcheck archive -------------------------------------------

    async def _post_or_update_archive_confirmation(self, guild, initiator, moved, failed):
        """Posts (or, on repeat runs, edits in place) a single confirmation
        message in the review channel - only one such message should ever
        be visible at a time, so mods always see the latest archive run."""
        review_channel = self.bot.get_channel(self.review_channel_id)
        if review_channel is None:
            return

        now = discord.utils.utcnow().astimezone(AMSTERDAM_TZ)
        timestamp_str = now.strftime("%Y-%m-%d %H:%M %Z")
        archive_link = f"https://discord.com/channels/{guild.id}/{config.ARCHIVE_CHANNEL_ID}"

        content = (
            f"📦 **Archiving complete** - {moved} application(s) moved"
            + (f", {failed} failed (see console log)" if failed else "")
            + f"\nInitiated by {initiator.mention} at {timestamp_str}\n"
            + f"Archived applications: {archive_link}"
        )

        record = self.bot.store.get(ARCHIVE_CONFIRMATION_KEY)
        if record:
            try:
                message = await review_channel.fetch_message(record["message_id"])
                await message.edit(content=content)
                return
            except (discord.NotFound, discord.Forbidden):
                pass  # old confirmation is gone - fall through and post a fresh one

        message = await review_channel.send(content=content)
        self.bot.store.set(ARCHIVE_CONFIRMATION_KEY, message_id=message.id)

    @gearcheck_group.command(name="character", description="Gear-check any character from Blizzard (moderators)")
    @app_commands.describe(name="The character's name and surname, e.g. Eggmike Thewhite",
                           public="Post it in this channel instead of only showing it to you")
    async def gearcheck_character(self, interaction: discord.Interaction, name: str, public: bool = False):
        if not is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can gear-check characters.", ephemeral=True)
            return
        if getattr(self.bot, "blizzard", None) is None:
            await interaction.response.send_message(
                "Blizzard API keys aren't set up (Settings, General, Integrations).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=not public, thinking=True)
        class_names, _ = await self._class_names()
        try:
            summary = await gear.fetch(self.bot, wow_api.slug(name), class_names)
        except (gear.GearError, wow_api.WowApiError) as exc:
            await interaction.followup.send(f"Couldn't gear-check **{name.strip()}**: {exc}", ephemeral=True)
            return
        text = gear.render_lines(summary, await gear.item_icons(self.bot, summary))
        container = discord.ui.Container(accent_color=discord.Color(
            config.CLASS_COLORS.get(summary["class_name"], 0x2B2D31)))
        details = " · ".join(filter(None, [f"Level {summary['level']}", summary.get("race"), summary["class_name"],
                                           summary.get("guild")]))
        profile_url = self._profile_url(summary["slug"])
        title = f"[{summary['character']}]({profile_url})" if profile_url else summary["character"]
        container.add_item(discord.ui.TextDisplay(f"## {title}\n{details}\n-# Gear as of the last logout"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(text["overview"]))
        container.add_item(discord.ui.TextDisplay(_clip(text["gear"], 3000)))
        if text["flags"]:
            container.add_item(discord.ui.TextDisplay(text["flags"]))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        await interaction.followup.send(view=view, ephemeral=not public,
                                        allowed_mentions=discord.AllowedMentions.none())

    @gearcheck_group.command(
        name="archive", description="Move all approved/denied applications to the archive channel"
    )
    async def gearcheck_archive(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only moderators can archive applications.", ephemeral=True
            )
            return
        if not config.ARCHIVE_ENABLED or not config.ARCHIVE_CHANNEL_ID:
            await interaction.response.send_message(
                "Archiving isn't set up: turn on the Archive command and pick the archive channel in Settings "
                "(Features, Applications).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        archive_channel = self.bot.get_channel(config.ARCHIVE_CHANNEL_ID)
        if archive_channel is None:
            await interaction.followup.send(
                "Couldn't find the configured archive channel.", ephemeral=True
            )
            return
        review_channel = self.bot.get_channel(self.review_channel_id)
        if review_channel is None:
            await interaction.followup.send("Couldn't find the review channel.", ephemeral=True)
            return

        moved = 0
        failed = 0
        # oldest_first so the archive ends up in the same order the review
        # channel had (oldest applicant at the top, most recent at the
        # bottom) - the default is newest-first, which would reverse it.
        async for message in review_channel.history(limit=None, oldest_first=True):
            application = self.bot.store.get(message.id)
            if application is None or application.get("status") not in ("approved", "denied"):
                continue
            try:
                new_message = await archive_channel.send(
                    view=self._render_application_view(application, message_id=message.id)
                )
                self.bot.store.set(new_message.id, **application)
                self.bot.store.delete(message.id)
                await message.delete()
                moved += 1
                await asyncio.sleep(0.5)  # stay clear of rate limits on big batches
            except Exception:
                log.exception("Failed to archive application message %s", message.id)
                failed += 1

        await self._post_or_update_archive_confirmation(interaction.guild, interaction.user, moved, failed)

        summary = f"Archived {moved} application(s)."
        if failed:
            summary += f" {failed} failed - check the console log."
        await interaction.followup.send(summary, ephemeral=True)

    # --- Blizzard armory diagnostic -------------------------------------


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplyCog(bot))
