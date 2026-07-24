"""
Guild application feature.

Flow:
  1. /apply character:<name> - bot looks the character up on WarcraftLogs.
  2. If found, the bot asks the applicant (via ephemeral selects/modal) for
     their main role, main spec, any other specs they can play, and an
     optional note - since logs alone can't always tell us this reliably.
  3. The bot then offers to attach an optional gear screenshot (the
     applicant replies with an image, or clicks Skip).
  4. The bot posts a summary embed to the review channel with checkmark/X
     reactions.
  5. A moderator reacts to approve (assigns the Fresh role + DMs the
     applicant) or deny (DMs the applicant).

This is the whole feature in one file by design - adding a new, unrelated
feature later means adding a new cog file, not touching this one.
"""

import os
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import icons

log = logging.getLogger("wow-apply-bot.apply")

APPROVE_EMOJI = "✅"
DENY_EMOJI = "❌"

DIALOG_TIMEOUT = 300       # role/spec selection
SCREENSHOT_TIMEOUT = 180   # waiting for an optional screenshot reply
NOT_FOUND_TIMEOUT = 180    # waiting for the retry-name/continue-anyway choice


def _emoji_for(icon_str):
    """Converts a resolved icon string (e.g. '<:name:123>') into a
    PartialEmoji usable in SelectOption(emoji=...), or None if unavailable."""
    if not icon_str:
        return None
    try:
        return discord.PartialEmoji.from_str(icon_str)
    except Exception:
        return None


def _match_known_spec(class_name: str, wcl_spec_name):
    """
    Matches WCL's reported spec name against config.CLASS_SPECS for this
    class, ignoring spacing/casing differences (e.g. WCL might report
    "BeastMastery" while we display "Beast Mastery") - returns our
    canonical display name, or None if there's no confident match rather
    than guessing.
    """
    if not wcl_spec_name:
        return None
    normalized = wcl_spec_name.replace(" ", "").lower()
    for s in config.CLASS_SPECS.get(class_name, []):
        if s.replace(" ", "").lower() == normalized:
            return s
    return None


class RoleSpecView(discord.ui.View):
    """View letting the applicant pick their main role, main spec, and any
    other specs they can also play. 'Continue' moves on to the note/screenshot
    step. Works identically whether sent to a DM channel or a guild channel."""

    def __init__(self, user_id, guild, class_name, suggested_role=None, suggested_spec=None):
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

        specs = config.CLASS_SPECS.get(class_name, [])

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
        specs = config.CLASS_SPECS.get(self.class_name, [])
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
        label="Your exact in-game character name",
        required=True,
        max_length=32,
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
    """Shown when a character isn't found on WCL - lets the applicant either
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
    """Shown when continuing without WCL data - there's no logged class to
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


class ApplyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.review_channel_id = int(os.environ["REVIEW_CHANNEL_ID"])
        self.fresh_role_id = int(os.environ["FRESH_ROLE_ID"])
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.server_slug = os.environ["SERVER_SLUG"]
        self.server_region = os.environ.get("SERVER_REGION", "us")
        self.start_view = StartApplicationView(self)

    async def cog_load(self):
        # Registers the persistent buttons so they keep working across bot
        # restarts, on every previously-posted message.
        self.bot.add_view(self.start_view)
        template = self._render_application_view({
            "applicant_id": 0,
            "character_name": "template",
            "status": "pending",
        })
        self.bot.add_view(template)

    # --- embed/layout building -----------------------------------------

    def _format_boss_line(self, name, kills, pct) -> str:
        if kills == 0:
            return f"**{name}** — No logs"
        pct_str = f"{pct:.0f}%" if pct is not None else "?"
        return f"`{pct_str}` **{name}** — `{kills} kills`"

    def _build_current_tier_boss_list(self, tier_data) -> str:
        by_encounter_id = {}
        if tier_data:
            for b in tier_data.get("per_boss") or []:
                if b.get("encounter_id") is not None:
                    by_encounter_id[b["encounter_id"]] = b

        lines = []
        for boss_name, encounter_id in config.CURRENT_TIER["bosses"].items():
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
        if level is not None:
            if level == 70:
                lines.append("**Level:** 70")
            else:
                lines.append(f"**Level:** ⚠️ {level} (manually verify)")
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

        current_kills = current_tier_data.get("total_kills", 0) if current_tier_data else 0
        current_has_data = bool(
            current_tier_data and current_tier_data.get("best_performance_average") is not None
        )
        low_kill_count = current_has_data and current_kills < config.NEW_TIER_LOG_THRESHOLD
        no_current_data = not current_has_data

        prev_tier_data = None
        if (
            (low_kill_count or no_current_data)
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
        if low_kill_count and prev_tier_data:
            tier_lines.append(
                f"⚠️ **Limited current-tier logs** - this character has only {current_kills} "
                f"kill(s) logged in {config.CURRENT_TIER['name']}, so {config.PREVIOUS_TIER['name']} "
                "data is also included below."
            )
            tier_lines.append(
                f"**Best Avg ({config.CURRENT_TIER['name']}):** "
                f"{current_tier_data['best_performance_average']:.1f}%"
            )
            tier_lines.append(
                f"**Median Avg ({config.CURRENT_TIER['name']}):** "
                f"{current_tier_data['median_performance_average']:.1f}%"
            )
            tier_lines.append(
                f"**Best Avg ({config.PREVIOUS_TIER['name']}):** "
                f"{prev_tier_data['best_performance_average']:.1f}%"
            )
            tier_lines.append(
                f"**Median Avg ({config.PREVIOUS_TIER['name']}):** "
                f"{prev_tier_data['median_performance_average']:.1f}%"
            )
        elif no_current_data and prev_tier_data:
            tier_lines.append(
                f"**Best Avg ({config.PREVIOUS_TIER['name']}):** "
                f"{prev_tier_data['best_performance_average']:.1f}%"
            )
            tier_lines.append(
                f"**Median Avg ({config.PREVIOUS_TIER['name']}):** "
                f"{prev_tier_data['median_performance_average']:.1f}%"
            )
        elif current_has_data:
            tier_lines.append(
                f"**Best Avg ({config.CURRENT_TIER['name']}):** "
                f"{current_tier_data['best_performance_average']:.1f}%"
            )
            tier_lines.append(
                f"**Median Avg ({config.CURRENT_TIER['name']}):** "
                f"{current_tier_data['median_performance_average']:.1f}%"
            )

        tier_block = "\n".join(tier_lines) if tier_lines else None
        boss_block = (
            f"**Per-boss best parse & kills ({config.CURRENT_TIER['name']})**\n"
            + self._build_current_tier_boss_list(current_tier_data)
        )
        hidden_block = None
        if character.get("hidden"):
            hidden_block = "⚠️ **Note:** This character's logs are marked hidden/private on WarcraftLogs."

        return tier_block, boss_block, hidden_block

    def _render_application_view(self, application: dict) -> discord.ui.LayoutView:
        """
        Rebuilds the whole application layout from stored data - always
        from scratch, never by mutating an existing message's components.
        Used both for the initial post AND every subsequent edit (approve,
        deny, reset, /update-application), so there's exactly one code path
        that knows how to render an application, and no dependency on
        component-mutation APIs.
        """
        view = discord.ui.LayoutView(timeout=None)
        color = discord.Color(application.get("class_color") or 0x2B2D31)
        container = discord.ui.Container(accent_color=color)

        status = application.get("status", "pending")
        suffix = {"approved": " (APPROVED)", "denied": " (DENIED)"}.get(status, "")
        profile_url = application.get("profile_url") or ""
        character_name = application.get("character_name", "?")
        title = f"## {character_name}{suffix}"
        if profile_url:
            title = f"## [{character_name}{suffix}]({profile_url})"
        container.add_item(discord.ui.TextDisplay(title))
        container.add_item(discord.ui.TextDisplay(f"Applicant: <@{application['applicant_id']}>"))
        container.add_item(discord.ui.Separator())

        prior_denials = [
            (mid, rec)
            for mid, rec in self.bot.store.find_all_by_applicant(application["applicant_id"])
            if rec.get("status") == "denied"
        ]
        if prior_denials:
            most_recent_id, most_recent_rec = prior_denials[-1]
            most_recent_date = discord.utils.snowflake_time(most_recent_id).strftime("%Y-%m-%d")
            times = "time" if len(prior_denials) == 1 else "times"
            container.add_item(discord.ui.TextDisplay(
                f"⚠️ **Previously Denied** ⚠️\nThis character has been denied {len(prior_denials)} "
                f"{times} before. Most recent: **{most_recent_rec.get('character_name', '?')}** "
                f"on {most_recent_date}."
            ))
            container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(application.get("class_block") or "-"))
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(
            f"**Can also play:** {application.get('also_play_block') or '-'}"
        ))
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

        if profile_url:
            container.add_item(discord.ui.TextDisplay(f"**Full profile:** {profile_url}"))
            container.add_item(discord.ui.Separator())

        if application.get("note"):
            container.add_item(discord.ui.TextDisplay(
                f"**Applicant's note:**\n{application['note'][:1024]}"
            ))
            container.add_item(discord.ui.Separator())

        screenshot_urls = (application.get("screenshot_urls") or [])[: config.MAX_SCREENSHOTS]
        if screenshot_urls:
            gallery = discord.ui.MediaGallery(
                *[discord.MediaGalleryItem(u) for u in screenshot_urls]
            )
            container.add_item(gallery)
            container.add_item(discord.ui.Separator())

        footer_text = application.get("decision_footer") or (
            f"React {APPROVE_EMOJI} to approve or {DENY_EMOJI} to deny"
        )
        container.add_item(discord.ui.TextDisplay(footer_text))

        view.add_item(container)

        action_row = discord.ui.ActionRow()
        reset_button = discord.ui.Button(
            label="🔄 Reset", style=discord.ButtonStyle.danger, custom_id="wow_apply_reset"
        )
        reset_button.callback = self._on_reset_click
        action_row.add_item(reset_button)
        view.add_item(action_row)

        return view

    async def _refresh_review_message(self, message: discord.Message, application: dict):
        await message.edit(view=self._render_application_view(application))

    async def _post_application(
        self, interaction, char_data, role, spec, other_specs, note, screenshot_urls
    ):
        class_block = self._build_class_block(
            interaction.guild, char_data["class_name"], spec, role,
            char_data.get("level"), char_data.get("guild_name"),
        )
        also_play_block = self._build_also_play_block(char_data["class_name"], other_specs)
        tier_block, boss_block, hidden_block = await self._compute_tier_and_boss_blocks(char_data)

        application = {
            "applicant_id": interaction.user.id,
            "character_name": char_data["name"],
            "status": "pending",
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
            "screenshot_urls": screenshot_urls or [],
        }

        review_channel = self.bot.get_channel(self.review_channel_id)
        view = self._render_application_view(application)
        message = await review_channel.send(view=view)
        await message.add_reaction(APPROVE_EMOJI)
        await message.add_reaction(DENY_EMOJI)

        self.bot.store.add(
            message.id,
            application["applicant_id"],
            application["character_name"],
            status="pending",
            role=role,
            spec=spec,
            other_specs=other_specs,
            note=note,
            class_color=application["class_color"],
            last_decision=None,
            class_block=class_block,
            also_play_block=also_play_block,
            tier_block=tier_block,
            boss_block=boss_block,
            hidden_block=hidden_block,
            profile_url=application["profile_url"],
            screenshot_urls=application["screenshot_urls"],
        )

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

    async def _suggest_spec_role(self, char_data):
        suggested_spec = None
        if char_data.get("id") and config.CURRENT_TIER.get("zone_id"):
            try:
                tier_data = await self.bot.wcl.get_tier_data(
                    char_data["id"], config.CURRENT_TIER["zone_id"]
                )
                if tier_data:
                    suggested_spec = _match_known_spec(char_data["class_name"], tier_data.get("primary_spec"))
            except Exception:
                log.exception("Failed to fetch current-tier data for spec suggestion")

        if not suggested_spec and char_data.get("id") and config.PREVIOUS_TIER and config.PREVIOUS_TIER.get("zone_id"):
            try:
                prev_tier_data = await self.bot.wcl.get_tier_data(
                    char_data["id"], config.PREVIOUS_TIER["zone_id"]
                )
                if prev_tier_data:
                    suggested_spec = _match_known_spec(char_data["class_name"], prev_tier_data.get("primary_spec"))
            except Exception:
                log.exception("Failed to fetch previous-tier data for spec suggestion")

        suggested_role = config.SPEC_DEFAULT_ROLE.get(suggested_spec, "DPS") if suggested_spec else None
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
            "**This step is optional:**\n\n"
            "You can provide a note and/or screenshot(s) of your gear by simply replying to this chat.\n"
            "This information will be added added to your application and can be seen by the reviewers\n\n"
            "Your application will automatically post in 3 minutes. You may press the skip button to immediately continue\n\n"
            ""
            "**At any time during the review, you can reply with `/update-application` to be able to add/change your note/screenshots**",
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
                await dm_channel.send(
                    "⏱️ Time's up - I didn't receive a note or screenshot in time, but no "
                    "worries: your application has still been submitted to the reviewers. "
                    "You can still add a note or screenshot(s) later with `/update-application`."
                )
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

    async def _run_application_flow(self, interaction: discord.Interaction, character: str):
        try:
            char_data = await self.bot.wcl.get_character(character, self.server_slug, self.server_region)
        except Exception:
            log.exception("WarcraftLogs lookup failed")
            await interaction.followup.send(
                "Something went wrong looking that character up on WarcraftLogs. "
                "Please try again in a minute, or let a moderator know.",
                ephemeral=True,
            )
            return

        if char_data is None:
            await self._handle_not_found(interaction, character)
            return

        await self._continue_with_found_character(interaction, char_data)

    async def _handle_not_found(self, interaction: discord.Interaction, character: str):
        view = NotFoundView(self, interaction.user.id, character)
        await interaction.followup.send(
            f"I couldn't find **{character}** on fresh.warcraftlogs.com for "
            f"realm `{self.server_slug}` ({self.server_region}). Double-check the "
            "spelling/capitalization and try again - or if that's definitely "
            "correct and you just don't have logs uploaded yet, you can continue "
            "anyway and a moderator will review manually.",
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

        # action == "continue": no WCL data means no known class to build a
        # spec dropdown from - ask for it manually, then everything else
        # (role/spec/note/screenshot) is identical to the found-character
        # flow, just with no pre-filled suggestions.
        class_view = ClassSelectView(interaction.user.id, interaction.guild)
        await interaction.followup.send(
            "Since there's no WCL data for this character, please select their class:",
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
            "class_name": class_view.class_name,
            "level": None,
            "hidden": False,
            "guild_name": "None",
            "best_performance_average": None,
            "median_performance_average": None,
            "profile_url": (
                f"https://fresh.warcraftlogs.com/character/{self.server_region}/"
                f"{self.server_slug}/{character}"
            ),
        }
        await self._continue_with_found_character(interaction, char_data)

    async def _continue_with_found_character(self, interaction: discord.Interaction, char_data: dict):
        suggested_spec, suggested_role = await self._suggest_spec_role(char_data)

        view = RoleSpecView(
            interaction.user.id,
            interaction.guild,
            char_data["class_name"],
            suggested_role=suggested_role,
            suggested_spec=suggested_spec,
        )
        suggestion_note = (
            " I've pre-selected a role and spec based on your logs - please confirm or change them."
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
                f"\nThanks! I found useful data on ``{char_data['name']}`` - please check your DMs to continue the process.",
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
            review_message = await review_channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
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

    # --- gear-check channel explainer ------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_gear_check_explainer()

    async def _ensure_gear_check_explainer(self):
        gear_check_channel_id = os.environ.get("GEAR_CHECK_CHANNEL_ID")
        if not gear_check_channel_id:
            return
        channel = self.bot.get_channel(int(gear_check_channel_id))
        if channel is None:
            log.warning("GEAR_CHECK_CHANNEL_ID set but channel not found/visible")
            return

        marker = "gear-check-explainer"
        try:
            pins = await channel.pins()
        except discord.Forbidden:
            log.warning("Missing permission to read pins in gear-check channel")
            return

        if any(m.embeds and m.embeds[0].footer.text == marker for m in pins):
            return  # already posted, nothing to do

        embed = discord.Embed(
            title="How to Gear-check and get the permissions to sign up for raids",
            description=(
                "Click the blue button below this message to start your gear-check process.\n\n"

                "1. You'll be asked for your character's exact in-game name,\n"
                "2. The bot will ask for your exact in-game name and fetch some data \n"
                "3. You will receive a DM, the bot makes a suggestion on what your main spec is (this can be changed)\n"
                "4. You are (optionally) given the ability to provide a note and/or screenshots\n"
                "5. You have the option to provide more information at any time by just replying to the bot\n"
                "\n\n"
                "Your character will be reviewed by our recruitment-team and will be notified of the outcome\n"
                "When approved, you will be granted the correct roles and further instructions on how to sign for the raid(s)"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=marker)

        try:
            message = await channel.send(embed=embed, view=self.start_view)
            await message.pin()
        except discord.Forbidden:
            log.warning("Missing permission to post/pin the explainer in gear-check channel")

    # --- moderator approve/deny/reset --------------------------------------

    async def _reactor_is_mod(self, guild, user_id) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    async def _on_reset_click(self, interaction: discord.Interaction):
        application = self.bot.store.get(interaction.message.id)
        if application is None:
            await interaction.response.send_message(
                "Couldn't find this application's record.", ephemeral=True
            )
            return
        if not await self._reactor_is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only moderators can reset an application.", ephemeral=True
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
            decision_footer=f"Reset by {interaction.user} - react to re-decide",
        )

        await interaction.message.clear_reactions()
        await interaction.message.add_reaction(APPROVE_EMOJI)
        await interaction.message.add_reaction(DENY_EMOJI)

        updated = self.bot.store.get(interaction.message.id)
        await interaction.message.edit(view=self._render_application_view(updated))

        await interaction.response.send_message(
            "Application reset - react with ✅ or ❌ to re-decide.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if payload.channel_id != self.review_channel_id:
            return
        if str(payload.emoji) not in (APPROVE_EMOJI, DENY_EMOJI):
            return

        application = self.bot.store.get(payload.message_id)
        if application is None or application["status"] != "pending":
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        if not await self._reactor_is_mod(guild, payload.user_id):
            return

        applicant = guild.get_member(application["applicant_id"])
        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        reviewer = guild.get_member(payload.user_id)
        character_name = application["character_name"]

        # None on a fresh application; "approved"/"denied" if this decision
        # follows a Reset - lets us know whether (and how) to notify the
        # applicant, per the correction rules below.
        previous_decision = application.get("last_decision")
        now = discord.utils.utcnow()
        timestamp_str = now.strftime("%Y-%m-%d %H:%M %z UTC-2")

        if str(payload.emoji) == APPROVE_EMOJI:
            assigned_roles = []
            if applicant is not None:
                role = guild.get_role(self.fresh_role_id)
                if role is not None:
                    try:
                        await applicant.add_roles(role, reason="Guild application approved")
                        assigned_roles.append(role.name)
                    except discord.Forbidden:
                        log.warning("Missing permission to assign role to %s", applicant)

                try:
                    await applicant.edit(nick=character_name, reason="Guild application approved")
                except discord.Forbidden:
                    log.warning("Missing permission to set nickname for %s", applicant)

                # Notify on a first-ever approval, or when correcting a
                # previous denial. Re-approving after an already-approved
                # state (double reset) has nothing new to tell them.
                should_dm = previous_decision is None or previous_decision == "denied"
                if should_dm:
                    try:
                        await applicant.send(
                        f"🎉 Your character **{application['character_name']}** "
                        "has been approved! Welcome to Crusader's PUG.\n\n" 

                        "You have been granted access to the raid-signup channel(s).\n" 
                        "Make sure to sign-up and confirm your spot when you are placed on the roster!\n\n"

                        "One more step: register your character for raid sign-ups with "
                        f"</register:{config.OXM_REGISTER_COMMAND_ID}> (click it, then press Enter) "
                        "- or just type `/register` yourself if the click doesn't pre-fill it.\n\n"

                        "Please note: your (server-specific) Discord nickname has been changed to your characters name."
                        )
                    except discord.Forbidden:
                        await channel.send(
                            f"(Couldn't DM {applicant.mention} - their DMs may be closed. "
                            "Please let them know they've been accepted.)"
                        )

            roles_str = ", ".join(assigned_roles) if assigned_roles else "None"
            self.bot.store.update(
                payload.message_id,
                status="approved",
                last_decision="approved",
                decision_footer=(
                    f"✅ Approved by {reviewer} • {timestamp_str}\nRole(s) assigned: {roles_str}"
                ),
            )

        else:  # DENY_EMOJI
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
                        await applicant.send(
                            f"Thanks for applying, **{character_name}**. "
                            "Unfortunately we won't be moving forward with your application "
                            "at this time. Feel free to reach out to an officer if you have "
                            "questions or come back later."
                        )
                    except discord.Forbidden:
                        await channel.send(
                            f"(Couldn't DM {applicant.mention} - their DMs may be closed. "
                            "Please let them know about the decision.)"
                        )

            self.bot.store.update(
                payload.message_id,
                status="denied",
                last_decision="denied",
                decision_footer=f"❌ Denied by {reviewer} • {timestamp_str}\nRoles assigned: None",
            )

        updated = self.bot.store.get(payload.message_id)
        await self._refresh_review_message(message, updated)


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplyCog(bot))