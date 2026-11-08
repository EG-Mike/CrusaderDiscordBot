"""
Guild applications.

Flow:
  1. The applicant clicks "Start application" in the apply channel (or
     uses /apply) and types their character's name and surname in one
     field. The bot reads the character from Blizzard's API (gear.py):
     class, level, guild, talents, statistics, and the equipped gear with
     enchants and gems.
  2. By DM: the main spec is pre-selected from where the talent points
     are, with a role to match; the applicant confirms or changes them and
     can pick an offspec (optional).
  3. An optional note and screenshots, collected by DM.
  4. The application is posted to the review channel: the gear summary,
     the statistics that matter for the role, requirements (Settings:
     minimum level and item level), and, once WarcraftLogs covers the game
     (APPLICATION_WARCRAFTLOGS_ENABLED), the applicant's parses. It also
     shows on the dashboard's Applications page.
  5. An approver approves (roles, nickname, DM, and the character is linked
     to the applicant on the guild roster with their specs) or denies (DM),
     on the card or on the dashboard. Reset undoes a decision, "Refresh
     gear" reads the gear from Blizzard again.

Decisions are methods without a Discord interaction (approve, deny,
reset, refresh_gear, archive_decided), so the card's buttons and the
dashboard (webapi/applications_api.py) follow the same rules. Shared
record helpers are in applications.py.

Moderators can also look up any character without an application: /lookup
(the dashboard's Character lookup does the same).
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import applications
import bot_messages
import config
import gear
import icons
import roster
import wow_api
from applications import ApplicationError, Reviewer
from permissions import is_moderator, member_is_moderator

log = logging.getLogger("wow-apply-bot.apply")

APPROVE_EMOJI = "✅"
DENY_EMOJI = "❌"
FRESH_ROLE_EMOJI = "<:fresh_role:1542590697238962226>"
NO_OFFSPEC = "__none__"

DIALOG_TIMEOUT = 300       # spec/role selection
SCREENSHOT_TIMEOUT = 180   # waiting for an optional note/screenshot reply
NOT_FOUND_TIMEOUT = 180    # waiting for the retry-name/continue-anyway choice

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

ARCHIVE_CONFIRMATION_KEY = "archive_confirmation"  # sentinel store key, not a real message id
CARD_TEXT_LIMIT = 3900  # Discord allows 4000 characters of text per message; a little room to spare
MIN_GEAR_TEXT = 600     # the gear list always gets at least this much
BLIZZARD_ROLES = {"tank": "Tank", "healer": "Healer", "dps": "DPS"}  # wow_api spec roles -> ours


def _fresh_label(name: str) -> str:
    """Prefixes the Fresh role's emoji in front of its name wherever it's
    mentioned in an assigned/already-had role list - other exempt roles
    (Regular/Organizer) are left as plain text."""
    return f"{FRESH_ROLE_EMOJI} {name}" if name == "Fresh" else name


def _clip(text: str, limit: int) -> str:
    """Cuts at a line break so an item line isn't split."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 40)
    return text[:cut if cut > 0 else limit - 40] + "\n-# … more items on the dashboard's Applications page"


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


def default_role(class_name: str, spec: Optional[str], spec_roles: Dict[str, Optional[str]]) -> Optional[str]:
    """The role a spec usually plays: Blizzard's role for the spec when it
    gives one, else SPEC_DEFAULT_ROLE (healing and tanking trees), else DPS.
    None when the class can't play that role."""
    if not spec:
        return None
    allowed = config.CLASS_ROLES.get(class_name, config.ROLES)
    role = BLIZZARD_ROLES.get(spec_roles.get(spec) or "") or config.SPEC_DEFAULT_ROLE.get(spec) or "DPS"
    return role if role in allowed else None


def screenshot_expiry(url: str) -> Optional[int]:
    """When a Discord attachment link stops working (its `ex` parameter,
    unix seconds in hex). None for links without one."""
    try:
        values = parse_qs(urlparse(url).query).get("ex")
        return int(values[0], 16) if values else None
    except (ValueError, TypeError):
        return None


class SpecChoiceView(discord.ui.View):
    """The applicant confirms their main spec (pre-selected from their
    talent points) and role, and can pick an offspec. Works the same in a
    DM and in a server channel. `specs`: the class's spec names for the game
    version; `spec_roles`: Blizzard's role per spec name (may be empty)."""

    def __init__(self, user_id, guild, class_name, specs, spec_roles=None, suggested_spec=None,
                 suggested_role=None):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.user_id = user_id
        self.guild = guild
        self.class_name = class_name
        self.specs = list(specs)
        self.spec_roles = spec_roles or {}
        self.main_spec = suggested_spec
        self.role = suggested_role
        self.role_chosen = False  # once the applicant picks a role, changing the spec doesn't override it
        self.offspec = None
        self.done = asyncio.Event()

        self.spec_select = discord.ui.Select(placeholder="Main spec", row=0)
        self.spec_select.callback = self._on_spec
        self.add_item(self.spec_select)

        self.role_select = discord.ui.Select(placeholder="Main role", row=1)
        self.role_select.callback = self._on_role
        self.add_item(self.role_select)

        self.offspec_select = None
        if len(self.specs) > 1:
            self.offspec_select = discord.ui.Select(placeholder="Offspec (optional)", row=2)
            self.offspec_select.callback = self._on_offspec
            self.add_item(self.offspec_select)

        continue_button = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary, row=3)
        continue_button.callback = self._on_continue
        self.add_item(continue_button)
        self._sync()

    def _sync(self):
        """Rebuilds every dropdown from the current choices (Discord resends
        all components on an edit, so each option's `default` must be right)."""
        self.spec_select.options = [
            discord.SelectOption(label=s, emoji=_emoji_for(icons.resolve_spec_icon(self.class_name, s)),
                                 default=(s == self.main_spec))
            for s in self.specs
        ] or [discord.SelectOption(label="(no specs found)", value=NO_OFFSPEC)]
        self.role_select.options = [
            discord.SelectOption(label=r, emoji=_emoji_for(icons.resolve_role_icon(self.guild, r)),
                                 default=(r == self.role))
            for r in config.CLASS_ROLES.get(self.class_name, config.ROLES)
        ]
        if self.offspec_select is not None:
            if self.offspec == self.main_spec:
                self.offspec = None
            self.offspec_select.options = [
                discord.SelectOption(label="No offspec", value=NO_OFFSPEC, default=self.offspec is None)
            ] + [
                discord.SelectOption(label=s, emoji=_emoji_for(icons.resolve_spec_icon(self.class_name, s)),
                                     default=(s == self.offspec))
                for s in self.specs if s != self.main_spec
            ]

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your application.", ephemeral=True)
            return False
        return True

    async def _on_spec(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        value = self.spec_select.values[0]
        self.main_spec = value if value != NO_OFFSPEC else None
        if not self.role_chosen:
            self.role = default_role(self.class_name, self.main_spec, self.spec_roles) or self.role
        self._sync()
        await interaction.response.edit_message(view=self)

    async def _on_role(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.role = self.role_select.values[0]
        self.role_chosen = True
        self._sync()
        await interaction.response.edit_message(view=self)

    async def _on_offspec(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        value = self.offspec_select.values[0]
        self.offspec = None if value == NO_OFFSPEC else value
        self._sync()
        await interaction.response.edit_message(view=self)

    async def _on_continue(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        if not self.role or not self.main_spec:
            await interaction.response.send_message("Please pick your main spec and role first.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
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
            "**You can add a note and/or screenshot(s) at any time with `/update-application` in this chat.**",
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
    Persistent view for the apply channel's pinned explainer. The button
    click itself only needs View Channel (unlike typing a slash command,
    which Discord requires Send Messages for) - so this works even in a
    channel where @everyone can't post, keeping it clutter-free.
    """

    def __init__(self, cog: "ApplyCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Start application",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="wow_apply_start",  # unchanged, so pinned messages from before keep working
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
    infer from, so the applicant picks it before the spec dialog."""

    def __init__(self, user_id, guild, class_names: List[str]):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.user_id = user_id
        self.class_name = None
        self.done = asyncio.Event()

        select = discord.ui.Select(
            placeholder="What class is your character?",
            options=[
                discord.SelectOption(label=c, emoji=_emoji_for(icons.resolve_class_icon(guild, c)))
                for c in class_names[:25]
            ],
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your application.", ephemeral=True)
            return
        self.class_name = self._select.values[0]
        await interaction.response.defer()
        self.done.set()
        self.stop()


class DenyReasonModal(discord.ui.Modal, title="Deny application"):
    reason = discord.ui.TextInput(
        label="Reason (optional, reviewers only)",
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
        try:
            await self.cog.deny(self.message_id, applications.discord_reviewer(interaction.user),
                                str(self.reason).strip() or None)
        except ApplicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)


class ApplyCog(commands.Cog):
    applications_group = app_commands.Group(
        name="applications", description="Guild applications (moderators)"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.start_view = StartApplicationView(self)
        self._locks: Dict[int, asyncio.Lock] = {}
        self._screenshot_cache: Dict[str, Tuple[str, int]] = {}  # original link -> (fresh link, expires)

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
        bot_messages.register_updater("gear_check_explainer", self._sync_apply_explainer)
        bot_messages.register_updater("review_explainer", self._sync_review_explainer)

    # --- the review card ------------------------------------------------------------------------------

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

    def _build_class_block(self, guild, class_name, spec, role, level, guild_name, offspec=None) -> str:
        class_icon = icons.resolve_class_icon(guild, class_name)
        lines = [f"**Class:** {class_icon} {class_name}".strip()]
        if spec:
            spec_icon = icons.resolve_spec_icon(class_name, spec)
            lines.append(f"**Spec:** {spec_icon} {spec}".strip())
        if role:
            role_icon = icons.resolve_role_icon(guild, role)
            lines.append(f"**Role:** {role_icon} {role}".strip())
        if offspec:
            lines.append(f"**Offspec:** {icons.resolve_spec_icon(class_name, offspec)} {offspec}".strip())
        lines.append(f"**Level:** {level if level is not None else '? (not found on Blizzard)'}")
        lines.append(f"**Guild:** {guild_name or 'None'}")
        return "\n".join(lines)

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
        WarcraftLogs parses (APPLICATION_WARCRAFTLOGS_ENABLED), read once when
        the application is posted. Everything rendered afterward reuses the
        resulting text, so a decision doesn't wait on the API and a rebuild
        can't silently change the numbers shown.
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
        # Fewer than NEW_TIER_LOG_THRESHOLD full clears - including zero - is
        # what triggers the previous-tier fallback.
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
        Rebuilds the whole review card from stored data - always from
        scratch, for the first post and every later edit (decisions, gear
        refresh, /update-application), so exactly one code path knows how to
        render an application.

        message_id, if given, is this application's own message id - used to
        leave it out when counting earlier denials.
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

        prior_denials = applications.prior_denials(self.bot.store, application["applicant_id"], exclude=message_id)
        # Only worth flagging once it's a pattern, not a single past denial.
        if len(prior_denials) >= 2:
            most_recent_id, most_recent_rec = prior_denials[-1]
            most_recent_date = time.strftime(
                "%Y-%m-%d", time.gmtime(applications.submitted_at(most_recent_id, most_recent_rec)))
            container.add_item(discord.ui.TextDisplay(
                f"⚠️ **Previously Denied** ⚠️\nThis applicant has been denied {len(prior_denials)} "
                f"times before. Most recent: **{most_recent_rec.get('character_name', '?')}** "
                f"on {most_recent_date}."
            ))
            container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(application.get("class_block") or "-"))
        container.add_item(discord.ui.Separator())

        if application.get("also_play_block") and application["also_play_block"] != "-":
            # Applications from before the offspec choice listed every other spec here.
            container.add_item(discord.ui.TextDisplay(f"**Can also play:** {application['also_play_block']}"))
            container.add_item(discord.ui.Separator())

        gear_text = application.get("gear_text")
        if gear_text:
            checked = application.get("gear_checked_at")
            when = f" · checked <t:{checked}:R>" if checked else ""
            container.add_item(discord.ui.TextDisplay(
                f"### Character\n-# From Blizzard, as of the character's last logout{when}\n{gear_text['overview']}"))
            container.add_item(discord.ui.TextDisplay(_clip(gear_text["gear"], self._gear_room(application))))
            if gear_text.get("flags"):
                container.add_item(discord.ui.TextDisplay(gear_text["flags"]))
            container.add_item(discord.ui.Separator())
        elif application.get("armory_block"):  # applications from before Blizzard's WoW Forever data
            container.add_item(discord.ui.TextDisplay(application["armory_block"]))
            container.add_item(discord.ui.Separator())

        for key in ("tier_block", "boss_block", "hidden_block"):
            if application.get(key):
                container.add_item(discord.ui.TextDisplay(application[key]))
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
            container.add_item(discord.ui.MediaGallery(*[discord.MediaGalleryItem(u) for u in screenshot_urls]))
        else:
            container.add_item(discord.ui.TextDisplay("**Screenshot(s):** No screenshot provided"))
        container.add_item(discord.ui.Separator())

        footer_text = application.get("decision_footer") or "Use the buttons below or the dashboard to decide"
        container.add_item(discord.ui.TextDisplay(footer_text))

        view.add_item(container)

        action_row = discord.ui.ActionRow()
        # The custom IDs are unchanged from the first version, so every card ever posted keeps working.
        for label, emoji, style, custom_id, disabled, callback in (
            ("Approve", APPROVE_EMOJI, discord.ButtonStyle.success, "wow_apply_approve", not is_pending,
             self._on_approve_click),
            ("Deny", DENY_EMOJI, discord.ButtonStyle.danger, "wow_apply_deny", not is_pending, self._on_deny_click),
            ("Reset", "🔄", discord.ButtonStyle.primary, "wow_apply_reset", is_pending, self._on_reset_click),
            ("Refresh from Blizzard", "🔃", discord.ButtonStyle.secondary, "wow_apply_refresh_gear",
             not application.get("character_slug"), self._on_refresh_gear_click),
        ):
            button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=custom_id, disabled=disabled)
            button.callback = callback
            action_row.add_item(button)
        view.add_item(action_row)
        return view

    @staticmethod
    def _gear_room(application: dict) -> int:
        """How much of the card's text the gear list may use: what the rest
        of the card leaves (the note is cut at 1024 characters)."""
        gear_text = application.get("gear_text") or {}
        others = [application.get("character_name") or "", application.get("profile_url") or "",
                  application.get("class_block") or "", application.get("also_play_block") or "",
                  gear_text.get("overview") or "", gear_text.get("flags") or "", application.get("tier_block") or "",
                  application.get("boss_block") or "", application.get("hidden_block") or "",
                  application.get("wcl_url") or "", (application.get("note") or "")[:1024],
                  application.get("decision_footer") or ""]
        fixed = 600  # headings, the applicant line, warnings, labels and separators
        return max(MIN_GEAR_TEXT, CARD_TEXT_LIMIT - fixed - sum(len(t) for t in others))

    async def _gear_fields(self, summary, role=None) -> dict:
        """What an application stores about the gear: the summary (data) and
        its rendered text, so a re-render never needs Blizzard."""
        if not summary:
            return {"gear": None, "gear_text": None, "gear_checked_at": None}
        icons_by_item = await gear.item_icons(self.bot, summary)
        return {"gear": summary, "gear_text": gear.render_lines(summary, icons_by_item, role=role),
                "gear_checked_at": int(time.time())}

    # --- where an application's card is ---------------------------------------------------------------

    def _lock(self, message_id: int) -> asyncio.Lock:
        """One decision at a time per application (two reviewers clicking at once)."""
        return self._locks.setdefault(int(message_id), asyncio.Lock())

    def _guild(self):
        channel = self.bot.get_channel(config.REVIEW_CHANNEL_ID) if config.REVIEW_CHANNEL_ID else None
        if channel is not None and getattr(channel, "guild", None) is not None:
            return channel.guild
        return self.bot.guilds[0] if self.bot.guilds else None

    def _card_channel(self, record: dict):
        channel_id = record.get("channel_id") or config.REVIEW_CHANNEL_ID
        return self.bot.get_channel(int(channel_id)) if channel_id else None

    async def _card_message(self, message_id: int, record: dict) -> Optional[discord.Message]:
        channel = self._card_channel(record)
        if channel is None:
            return None
        try:
            return await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden):
            return None
        except discord.HTTPException:
            log.warning("Couldn't fetch the review card %s", message_id, exc_info=True)
            return None

    async def _rerender(self, message_id: int, message: Optional[discord.Message] = None):
        """Redraws the card from the stored record and tells the dashboard."""
        record = self.bot.store.get(message_id)
        if record is not None:
            message = message or await self._card_message(message_id, record)
            if message is not None:
                try:
                    await message.edit(view=self._render_application_view(record, message_id=int(message_id)))
                except discord.HTTPException:
                    log.warning("Couldn't update the review card %s", message_id, exc_info=True)
            else:
                log.warning("The review card of %s's application (%s) is gone; only the record was updated",
                            record.get("character_name"), message_id)
        applications.changed(message_id)

    async def _notice(self, record: dict, text: str):
        """A note for the reviewers next to the card (e.g. a DM that couldn't be sent)."""
        channel = self._card_channel(record)
        if channel is None:
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Couldn't post a notice in the review channel", exc_info=True)

    async def _applicant(self, guild, user_id: int) -> Optional[discord.Member]:
        if guild is None:
            return None
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.HTTPException:
                return None
        return member

    async def screenshot_urls(self, record: dict) -> List[str]:
        """The application's screenshots with links that work now: Discord's
        attachment links expire after about a day, so expired ones are
        renewed (Discord's refresh-urls endpoint). Links that can't be
        renewed are returned as they are."""
        urls = list(record.get("screenshot_urls") or [])
        now = int(time.time())

        def usable(url):
            cached = self._screenshot_cache.get(url)
            if cached and cached[1] > now + 300:
                return cached[0]
            expires = screenshot_expiry(url)
            return url if expires is None or expires > now + 300 else None

        stale = [u for u in urls if usable(u) is None]
        if stale:
            try:
                answer = await self.bot.http.request(discord.http.Route("POST", "/attachments/refresh-urls"),
                                                     json={"attachment_urls": stale})
                for pair in (answer or {}).get("refreshed_urls") or []:
                    if pair.get("refreshed"):
                        self._screenshot_cache[pair.get("original")] = (
                            pair["refreshed"], screenshot_expiry(pair["refreshed"]) or now + 3600)
            except discord.HTTPException:
                log.warning("Couldn't renew screenshot links", exc_info=True)
        return [usable(u) or u for u in urls]

    # --- decisions (Discord buttons and the dashboard) ------------------------------------------------

    def _pending(self, message_id: int) -> dict:
        record = self.bot.store.get(message_id)
        if not applications.is_application(record):
            raise ApplicationError("That application doesn't exist (any more).")
        if record["status"] != "pending":
            raise ApplicationError(f"This application was already {record['status']}. Reset it first to change "
                                   "the decision.")
        return record

    @staticmethod
    def _timestamp() -> str:
        return discord.utils.utcnow().astimezone(AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M %Z")

    async def _grant_role(self, guild, applicant, role_id, label, newly_assigned, already_had, reason):
        """Assigns role_id to applicant if not already present, tracking the
        outcome in the newly_assigned/already_had lists (both hold
        display labels, e.g. an icon + name string)."""
        if not role_id:
            return
        role = guild.get_role(int(role_id))
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
        icon = icons.resolve_class_icon(guild, name) if is_class else icons.resolve_role_icon(guild, name)
        return f"{icon} {name}".strip()

    async def approve(self, message_id: int, reviewer: Reviewer) -> dict:
        """Approves a pending application: the approval role (unless they
        already hold an exempt role), class and role roles, nickname, DM,
        and the character linked to them on the guild roster with their
        specs. Returns the updated record."""
        message_id = int(message_id)
        async with self._lock(message_id):
            application = self._pending(message_id)
            guild = self._guild()
            applicant = await self._applicant(guild, application["applicant_id"])
            character_name = application["character_name"]
            class_name = application.get("class_name")
            role_name = application.get("role")
            previous_decision = application.get("last_decision")
            reason = f"Guild application approved by {reviewer.label}"

            newly_assigned, already_had = [], []
            nickname_line = "Name not changed - the applicant isn't in the server"
            if applicant is not None:
                applicant_role_ids = {r.id for r in applicant.roles}
                held_exempt_roles = [name for rid, name in config.FRESH_EXEMPT_ROLE_IDS.items()
                                     if int(rid) in applicant_role_ids]
                is_exempt = bool(held_exempt_roles)
                held_exempt_display = "/".join(_fresh_label(name) for name in held_exempt_roles)

                if is_exempt:
                    already_had.append(held_exempt_display)
                else:
                    await self._grant_role(guild, applicant, config.FRESH_ROLE_ID, _fresh_label("Fresh"),
                                           newly_assigned, already_had, reason)

                # Class/role tags are independent of the exemption: a Regular still gets tagged.
                if role_name and role_name in config.AUTO_ROLE_IDS:
                    await self._grant_role(guild, applicant, config.AUTO_ROLE_IDS[role_name],
                                           self._class_or_role_label(guild, role_name, is_class=False),
                                           newly_assigned, already_had, reason)
                if class_name and class_name in config.AUTO_ROLE_IDS:
                    await self._grant_role(guild, applicant, config.AUTO_ROLE_IDS[class_name],
                                           self._class_or_role_label(guild, class_name, is_class=True),
                                           newly_assigned, already_had, reason)

                original_name = applicant.display_name
                if is_exempt:
                    nickname_line = f"Name not changed - already {held_exempt_display} ({original_name})"
                else:
                    try:
                        await applicant.edit(nick=character_name[:32], reason=reason)
                        nickname_line = f"Name changed: {original_name} → {character_name}"
                    except discord.Forbidden:
                        log.warning("Missing permission to set nickname for %s", applicant)
                        nickname_line = f"⚠️ Name NOT changed (missing permission) - still {original_name}"

                # Tell them on a first approval, or when correcting a denial. Re-approving
                # after an approval (reset twice) has nothing new to say.
                if previous_decision in (None, "denied"):
                    try:
                        await applicant.send(bot_messages.text("approval_dm", character=character_name))
                    except discord.HTTPException:
                        await self._notice(application, f"(Couldn't DM {applicant.mention} - their DMs may be "
                                                        "closed. Please let them know they've been accepted.)")

            roster_line = await self._link_on_roster(application)
            assigned_display = ", ".join(newly_assigned) if newly_assigned else "None"
            already_display = f"\nAlready had: {', '.join(already_had)}" if already_had else ""
            self.bot.store.update(
                message_id,
                status="approved",
                last_decision="approved",
                decision={"status": "approved", "by_id": reviewer.id, "by_name": reviewer.name, "via": reviewer.via,
                          "at": int(time.time()), "roles_assigned": newly_assigned, "roles_kept": already_had,
                          "nickname": nickname_line, "roster": roster_line, "reason": None},
                decision_footer=(
                    f"✅ Approved by {reviewer.label} • {self._timestamp()}\n"
                    f"Role(s) assigned: {assigned_display}{already_display}\n"
                    f"{nickname_line}" + (f"\n{roster_line}" if roster_line else "")
                ),
            )
        await self._rerender(message_id)
        return self.bot.store.get(message_id)

    async def _link_on_roster(self, application: dict) -> Optional[str]:
        """Links the approved character to the applicant on the guild roster
        (when the roster is on), with their main and offspec. When Blizzard
        doesn't know the character yet (below level 10), the link waits and
        is made as soon as it shows up (roster.remember_link), e.g. once it
        joins the guild. A line for the decision footer, or None."""
        character_slug = application.get("character_slug")
        if self.bot.get_cog("RosterCog") is None or not character_slug:
            return None
        spec_id, off_spec_id = application.get("spec_id"), application.get("off_spec_id")
        try:
            found = await roster.find_or_add(self.bot, character_slug)
        except roster.RosterError:
            if await asyncio.to_thread(roster.get, character_slug) is None:
                await asyncio.to_thread(roster.remember_link, character_slug, application["applicant_id"],
                                        by="application approval", main_spec_id=spec_id, off_spec_id=off_spec_id)
                return ("Guild roster: will be linked as soon as Blizzard shows the character (from level 10, or "
                        "once it's in the guild)")
            raise
        except Exception:
            log.exception("Linking %s on the guild roster failed", character_slug)
            return "Guild roster: not linked (see the System log)"
        try:
            await asyncio.to_thread(roster.link, found["slug"], application["applicant_id"],
                                    by="application approval", as_moderator=False)
        except roster.RosterError as exc:
            return f"Guild roster: not linked ({exc})"
        if spec_id:
            try:
                _, class_specs = await self._reference()
                spec_ids = {c_id: [s["id"] for s in specs] for c_id, specs in class_specs["by_id"].items()}
                await asyncio.to_thread(roster.set_specs, found["slug"], spec_id, off_spec_id,
                                        discord_id=application["applicant_id"], as_moderator=True,
                                        class_specs=spec_ids)
            except roster.RosterError as exc:
                log.info("Specs of %s not set on the roster: %s", found["slug"], exc)
            except Exception:
                log.exception("Setting the specs of %s on the guild roster failed", found["slug"])
        in_guild = " (shown on the posted roster once it's in the guild)" if not found.get("guild_slug") else ""
        return f"Guild roster: character linked to the applicant{in_guild}"

    async def deny(self, message_id: int, reviewer: Reviewer, reason: Optional[str] = None) -> dict:
        """Denies a pending application: removes the approval role (a
        correction of an approval) and DMs on a first denial only. The reason
        is for reviewers and is never sent to the applicant."""
        message_id = int(message_id)
        reason = (reason or "").strip()[:500] or None
        async with self._lock(message_id):
            application = self._pending(message_id)
            guild = self._guild()
            applicant = await self._applicant(guild, application["applicant_id"])
            if applicant is not None:
                role = guild.get_role(int(config.FRESH_ROLE_ID)) if config.FRESH_ROLE_ID else None
                if role is not None and role in applicant.roles:
                    try:
                        await applicant.remove_roles(role, reason=f"Guild application denied by {reviewer.label}")
                    except discord.Forbidden:
                        log.warning("Missing permission to remove role from %s", applicant)
                # A correction of an earlier approval just takes the role away, without a DM.
                if application.get("last_decision") is None:
                    try:
                        await applicant.send(bot_messages.text("denial_dm", character=application["character_name"]))
                    except discord.HTTPException:
                        await self._notice(application, f"(Couldn't DM {applicant.mention} - their DMs may be "
                                                        "closed. Please let them know about the decision.)")

            if application.get("character_slug") and application.get("last_decision") == "approved":
                await asyncio.to_thread(roster.forget_link, application["character_slug"])  # a link still waiting
            footer = f"❌ Denied by {reviewer.label} • {self._timestamp()}\nRoles assigned: None"
            if reason:
                footer += f"\nReason: {reason}"
            self.bot.store.update(
                message_id, status="denied", last_decision="denied", decision_footer=footer,
                decision={"status": "denied", "by_id": reviewer.id, "by_name": reviewer.name, "via": reviewer.via,
                          "at": int(time.time()), "roles_assigned": [], "roles_kept": [], "nickname": None,
                          "roster": None, "reason": reason},
            )
        await self._rerender(message_id)
        return self.bot.store.get(message_id)

    async def reset(self, message_id: int, reviewer: Reviewer) -> dict:
        """Puts a decided application back to pending. Roles stay as they
        are until the next decision."""
        message_id = int(message_id)
        async with self._lock(message_id):
            application = self.bot.store.get(message_id)
            if not applications.is_application(application):
                raise ApplicationError("That application doesn't exist (any more).")
            if application["status"] == "pending":
                raise ApplicationError("This application is already pending - nothing to reset.")
            self.bot.store.update(
                message_id, status="pending", last_decision=application["status"], decision=None,
                decision_footer=f"🔄 Reset by {reviewer.label} • {self._timestamp()} - decide again with the buttons "
                                "or on the dashboard",
            )
        await self._rerender(message_id)
        return self.bot.store.get(message_id)

    async def refresh_gear(self, message_id: int) -> dict:
        """Reads the character's gear from Blizzard again (it changes when the
        player logs out)."""
        message_id = int(message_id)
        application = self.bot.store.get(message_id)
        if not applications.is_application(application) or not application.get("character_slug"):
            raise ApplicationError("This application has no character to read from Blizzard.")
        if getattr(self.bot, "blizzard", None) is None:
            raise ApplicationError("Blizzard API keys aren't set up (Settings, General, Integrations).")
        class_names, _ = await self._reference()
        try:
            summary = await gear.fetch(self.bot, application["character_slug"], class_names)
        except (gear.GearError, wow_api.WowApiError) as exc:
            raise ApplicationError(f"Couldn't refresh the character: {exc}") from None
        self.bot.store.update(message_id, **await self._gear_fields(summary, application.get("role")))
        await self._rerender(message_id)
        return self.bot.store.get(message_id)

    # --- Discord buttons ------------------------------------------------------------------------------

    @staticmethod
    def is_approver(member) -> bool:
        """Who may approve, deny and reset: members with the approver role
        (Settings, Applications). Separate from moderators on purpose, so the
        review channel can be readable to more people than can decide."""
        return (isinstance(member, discord.Member) and bool(config.APPROVER_ROLE_ID)
                and any(role.id == int(config.APPROVER_ROLE_ID) for role in member.roles))

    async def _button_guard(self, interaction: discord.Interaction, *, decide: bool) -> Optional[dict]:
        application = self.bot.store.get(interaction.message.id)
        if not applications.is_application(application):
            await interaction.response.send_message("Couldn't find this application's record.", ephemeral=True)
            return None
        allowed = self.is_approver(interaction.user) if decide else (
            is_moderator(interaction.user) or self.is_approver(interaction.user))
        if not allowed:
            await interaction.response.send_message(
                "Only approvers can approve, deny or reset applications." if decide
                else "Only moderators and approvers can refresh the character.", ephemeral=True)
            return None
        return application

    async def _on_approve_click(self, interaction: discord.Interaction):
        if await self._button_guard(interaction, decide=True) is None:
            return
        await interaction.response.defer()  # silent: the card itself shows the result
        try:
            await self.approve(interaction.message.id, applications.discord_reviewer(interaction.user))
        except ApplicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def _on_deny_click(self, interaction: discord.Interaction):
        application = await self._button_guard(interaction, decide=True)
        if application is None:
            return
        if application["status"] != "pending":
            await interaction.response.send_message(
                "This application has already been decided - use Reset first if you need to change it.",
                ephemeral=True)
            return
        # A modal must be the first response, so the modal's on_submit does the rest.
        await interaction.response.send_modal(DenyReasonModal(self, interaction.message.id))

    async def _on_reset_click(self, interaction: discord.Interaction):
        if await self._button_guard(interaction, decide=True) is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.reset(interaction.message.id, applications.discord_reviewer(interaction.user))
        except ApplicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Application reset - Approve and Deny work again.", ephemeral=True)

    async def _on_refresh_gear_click(self, interaction: discord.Interaction):
        if await self._button_guard(interaction, decide=False) is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.refresh_gear(interaction.message.id)
        except ApplicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Refreshed from Blizzard (as of the character's last logout).",
                                        ephemeral=True)

    # --- applying -------------------------------------------------------------------------------------

    async def _reference(self) -> Tuple[Dict[int, str], dict]:
        """({class id: name}, {"by_name": {class: [spec]}, "by_id": {class id: [spec]}}) for the
        game version, from Blizzard. Each spec: {id, name, role}. Empty when
        Blizzard can't be reached."""
        try:
            ref = await wow_api.reference(self.bot)
        except wow_api.WowApiError:
            ref = {"classes": []}
        classes = ref.get("classes", [])
        return ({c["id"]: c["name"] for c in classes},
                {"by_name": {c["name"]: c["specs"] for c in classes}, "by_id": {c["id"]: c["specs"] for c in classes}})

    def _profile_url(self, character_slug: str):
        template = (config.GEARCHECK_PROFILE_URL or "").strip()
        if not template or not character_slug:
            return None
        return template.replace("{region}", wow_api.region()).replace("{slug}", character_slug)

    @app_commands.command(name="apply", description="Apply to the guild with your character")
    @app_commands.describe(character="Your character's name and surname, e.g. Eggmike Thewhite")
    async def apply(self, interaction: discord.Interaction, character: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._run_application_flow(interaction, character.strip())

    async def _run_application_flow(self, interaction: discord.Interaction, character: str):
        """The slash command and the Start button both end up here, after
        deferring their interaction."""
        if not config.REVIEW_CHANNEL_ID or self.bot.get_channel(config.REVIEW_CHANNEL_ID) is None:
            log.error("Can't take applications: the review channel isn't set (Settings, Applications)")
            await interaction.followup.send(
                "Sorry, applications can't be submitted right now (the review channel isn't set up). Please let a "
                "moderator know.", ephemeral=True)
            return
        if getattr(self.bot, "blizzard", None) is None:
            await self._handle_not_found(interaction, character)
            return
        class_names, _ = await self._reference()
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
        await self._choose_specs(interaction, char_data)

    async def _add_warcraftlogs_id(self, char_data: dict):
        """With WarcraftLogs parses on: the character's WarcraftLogs id and
        profile, for the tier and boss blocks. Never fails the application."""
        if not config.APPLICATION_WARCRAFTLOGS_ENABLED or getattr(self.bot, "wcl", None) is None:
            return
        try:
            found = await self.bot.wcl.get_character(char_data["name"], config.SERVER_SLUG, config.SERVER_REGION)
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
            "after logging out once. If the name is right, you can continue anyway and a reviewer will check "
            "your character by hand.",
            view=view,
            ephemeral=True,
        )
        try:
            await asyncio.wait_for(view.resolved.wait(), timeout=NOT_FOUND_TIMEOUT + 10)
        except asyncio.TimeoutError:
            return  # the buttons time out quietly; they can start again
        if view.action == "retry":
            return  # the button opened a new name form, which starts its own flow

        class_names, _ = await self._reference()
        class_view = ClassSelectView(interaction.user.id, interaction.guild,
                                     sorted(class_names.values()) or list(config.CLASS_SPECS.keys()))
        await interaction.followup.send("Since Blizzard has no data for this character, please pick its class:",
                                        view=class_view, ephemeral=True)
        try:
            await asyncio.wait_for(class_view.done.wait(), timeout=DIALOG_TIMEOUT + 10)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out waiting for the class - please start again.", ephemeral=True)
            return
        await self._choose_specs(interaction, {
            "id": None, "name": character, "slug": wow_api.slug(character), "class_name": class_view.class_name,
            "level": None, "guild_name": None, "profile_url": None, "gear": None,
        })

    async def _suggest_spec(self, char_data, specs: List[str]) -> Optional[str]:
        """The spec with the most talent points (Blizzard); with WarcraftLogs
        parses on and no talents spent, the spec logged most this tier."""
        suggested = (char_data.get("gear") or {}).get("suggested_spec")
        if suggested in specs:
            return suggested
        if config.APPLICATION_WARCRAFTLOGS_ENABLED and char_data.get("id"):
            for tier in (config.CURRENT_TIER, config.PREVIOUS_TIER):
                if not tier or not tier.get("zone_id"):
                    continue
                try:
                    tier_data = await self.bot.wcl.get_tier_data(char_data["id"], tier["zone_id"])
                except Exception:
                    log.exception("Failed to fetch tier data for the spec suggestion")
                    continue
                found = _match_known_spec(specs, (tier_data or {}).get("primary_spec"))
                if found:
                    return found
        return None

    async def _choose_specs(self, interaction: discord.Interaction, char_data: dict):
        """Main spec, role and offspec, by DM (or here when DMs are closed)."""
        _, class_specs = await self._reference()
        spec_objects = class_specs["by_name"].get(char_data["class_name"]) or [
            {"id": None, "name": s, "role": None} for s in config.CLASS_SPECS.get(char_data["class_name"], [])]
        specs = [s["name"] for s in spec_objects]
        spec_roles = {s["name"]: s.get("role") for s in spec_objects}
        suggested_spec = await self._suggest_spec(char_data, specs)
        view = SpecChoiceView(interaction.user.id, interaction.guild, char_data["class_name"], specs, spec_roles,
                              suggested_spec=suggested_spec,
                              suggested_role=default_role(char_data["class_name"], suggested_spec, spec_roles))

        summary = char_data.get("gear") or {}
        found = f"**{char_data['name']}**"
        if summary:
            found += f", level {summary.get('level') or '?'} {char_data['class_name']}"
            if summary.get("guild"):
                found += f" of <{summary['guild']}>"
        prompt_text = (
            f"Found {found}! "
            + (f"Your talent points say **{suggested_spec}**, so that's selected as your main spec. "
               if suggested_spec else "")
            + "Confirm or change your **main spec** and **role**, pick an **offspec** if you play one, "
              "then press **Continue**."
        )

        # The rest of the flow (specs, note, screenshot) happens in one DM
        # conversation. With DMs closed, the specs are asked here (only the
        # applicant sees it) and the note/screenshot step is skipped.
        dm_channel = await self._try_open_dm(interaction.user)
        if dm_channel is not None:
            try:
                await dm_channel.send(prompt_text, view=view)
            except discord.Forbidden:
                dm_channel = None
        if dm_channel is not None:
            await interaction.followup.send(f"Thanks! I found **{char_data['name']}** - please check your DMs to "
                                            "continue.", ephemeral=True)
        else:
            await interaction.followup.send(prompt_text, view=view, ephemeral=True)

        try:
            await asyncio.wait_for(view.done.wait(), timeout=DIALOG_TIMEOUT + 10)
        except asyncio.TimeoutError:
            await interaction.followup.send("The application timed out waiting for your spec - please start again.",
                                            ephemeral=True)
            return
        ids = {s["name"]: s.get("id") for s in spec_objects}
        choice = {"role": view.role, "spec": view.main_spec, "spec_id": ids.get(view.main_spec),
                  "offspec": view.offspec, "off_spec_id": ids.get(view.offspec) if view.offspec else None}
        await self._collect_note_screenshot_and_post(interaction, char_data, choice, dm_channel)

    async def _try_open_dm(self, user: discord.User):
        try:
            return await user.create_dm()
        except discord.HTTPException:
            return None

    async def _collect_note_screenshot_and_post(self, interaction, char_data, choice: dict, dm_channel):
        """Collects an optional note and screenshot(s) by DM, then posts the
        application. Without a DM channel it posts right away; the applicant
        can add them later with /update-application."""
        note_text, screenshot_urls = None, []
        if dm_channel is None:
            await interaction.followup.send(
                "I couldn't DM you to collect an optional note/screenshot - your DMs might be closed to server "
                "members. Posting your application without them for now; you can add them later with "
                "/update-application once you allow DMs.", ephemeral=True)
        else:
            skip_view = SkipNoteScreenshotView(interaction.user.id)
            await dm_channel.send(bot_messages.text("note_prompt_dm"), view=skip_view)

            def _is_applicant_dm_reply(m: discord.Message) -> bool:
                return m.author.id == interaction.user.id and m.channel.id == dm_channel.id

            message_task = asyncio.create_task(
                self.bot.wait_for("message", check=_is_applicant_dm_reply, timeout=SCREENSHOT_TIMEOUT))
            skip_task = asyncio.create_task(skip_view.skipped.wait())
            done, pending = await asyncio.wait({message_task, skip_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if message_task in done:
                try:
                    msg = message_task.result()
                    note_text = msg.content.strip() or None
                    screenshot_urls = [a.url for a in msg.attachments
                                       if (a.content_type or "").startswith("image/")][: config.MAX_SCREENSHOTS]
                except asyncio.TimeoutError:
                    try:
                        await dm_channel.send(bot_messages.text("note_timeout_dm"))
                    except discord.HTTPException:
                        pass
                except asyncio.CancelledError:
                    pass
        await self._post_application(interaction, char_data, choice, note_text, screenshot_urls)

    async def _post_application(self, interaction, char_data, choice: dict, note, screenshot_urls):
        offspec = choice.get("offspec")
        tier_block = boss_block = hidden_block = None
        if config.APPLICATION_WARCRAFTLOGS_ENABLED and char_data.get("id"):
            tier_block, boss_block, hidden_block = await self._compute_tier_and_boss_blocks(char_data)

        application = {
            "applicant_id": interaction.user.id,
            "character_name": char_data["name"],
            "character_slug": char_data.get("slug"),
            "status": "pending",
            "class_name": char_data["class_name"],
            "role": choice["role"],
            "spec": choice["spec"],
            "spec_id": choice.get("spec_id"),
            "other_specs": [offspec] if offspec else [],
            "off_spec_id": choice.get("off_spec_id"),
            "note": note,
            "class_color": config.CLASS_COLORS.get(char_data["class_name"], 0x2B2D31),
            "last_decision": None,
            "decision": None,
            "class_block": self._build_class_block(interaction.guild, char_data["class_name"], choice["spec"],
                                                   choice["role"], char_data.get("level"),
                                                   char_data.get("guild_name"), offspec),
            "tier_block": tier_block,
            "boss_block": boss_block,
            "hidden_block": hidden_block,
            "profile_url": char_data.get("profile_url"),
            "wcl_url": char_data.get("wcl_url"),
            "screenshot_urls": screenshot_urls or [],
            "channel_id": config.REVIEW_CHANNEL_ID,
            "submitted_at": int(time.time()),
            **await self._gear_fields(char_data.get("gear"), choice["role"]),
        }

        review_channel = self.bot.get_channel(config.REVIEW_CHANNEL_ID)
        if review_channel is None:
            log.error("Can't post %s's application: the review channel isn't set to a channel the bot can see",
                      application["character_name"])
            await interaction.followup.send(
                "Sorry, applications can't be submitted right now (the review channel isn't set up). "
                "Please let a moderator know.", ephemeral=True)
            return
        message = await review_channel.send(view=self._render_application_view(application))
        fields = {k: v for k, v in application.items() if k not in ("applicant_id", "character_name")}
        self.bot.store.add(message.id, application["applicant_id"], application["character_name"], **fields)
        applications.changed(message.id)

        try:
            await interaction.user.send(
                f"✅ Your application for **{application['character_name']}** has been posted for review. "
                "You'll get a DM once a reviewer makes a decision.")
        except discord.HTTPException:
            pass  # DMs closed - the confirmation below still covers this
        await interaction.followup.send(
            "Your application has been posted for review. You'll get a DM once a reviewer makes a decision.",
            ephemeral=True)

    # --- /update-application ---------------------------------------------------------------------------
    # The applicant usually can't see the review channel, so this works from
    # anywhere the bot can see them, DMs included.

    @app_commands.command(
        name="update-application",
        description="Add or change the note/screenshot(s) on your pending application",
    )
    async def update_application(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        mine = [(mid, rec) for mid, rec in applications.records(self.bot.store)
                if rec.get("applicant_id") == interaction.user.id]
        if not mine:
            await interaction.followup.send("You don't have an application yet - use the Start application button "
                                            "or /apply first.", ephemeral=True)
            return
        message_id, application = mine[0]  # newest
        if application["status"] != "pending":
            await interaction.followup.send(
                f"Your application has already been **{application['status']}** and can't be updated this way. "
                "If you'd like to be reconsidered, please reach out to an officer.", ephemeral=True)
            return

        await interaction.followup.send(
            f"Reply here with an updated note and/or screenshot(s) (up to {config.MAX_SCREENSHOTS} images) within "
            "3 minutes.", ephemeral=True)

        def check(m: discord.Message) -> bool:
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=SCREENSHOT_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out waiting for a reply - run /update-application again to retry.",
                                            ephemeral=True)
            return

        note_text = msg.content.strip() or None
        screenshot_urls = [a.url for a in msg.attachments
                           if (a.content_type or "").startswith("image/")][: config.MAX_SCREENSHOTS]
        if not note_text and not screenshot_urls:
            await interaction.followup.send("I didn't see a note or an image in that message - nothing updated.",
                                            ephemeral=True)
            return

        # A reviewer may have decided while we waited; never touch a decided application.
        application = self.bot.store.get(message_id)
        if application is None or application["status"] != "pending":
            await interaction.followup.send("Your application was decided while waiting for your reply, so this "
                                            "update wasn't applied.", ephemeral=True)
            return

        changes = {}
        if note_text:
            changes["note"] = note_text
        if screenshot_urls:
            changes["screenshot_urls"] = screenshot_urls
        self.bot.store.update(message_id, **changes)
        await self._rerender(message_id)

        updated_parts = (["note"] if note_text else []) + (
            [f"{len(screenshot_urls)} screenshot(s)"] if screenshot_urls else [])
        await interaction.followup.send(f"Updated: {', '.join(updated_parts)}.", ephemeral=True)

    # --- pinned explainers (texts editable on the dashboard) --------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("Applications: checking the apply and review channel explainers...")
        for key, updater in (("gear_check_explainer", self._sync_apply_explainer),
                             ("review_explainer", self._sync_review_explainer)):
            try:
                log.info("Applications: %s - %s", key, await updater())
            except bot_messages.BotMessageError as exc:
                log.warning("Applications: %s not posted: %s", key, exc)

    async def _sync_review_explainer(self) -> str:
        channel = self.bot.get_channel(config.REVIEW_CHANNEL_ID) if config.REVIEW_CHANNEL_ID else None
        return await bot_messages.sync_pinned(channel, "review_explainer")

    async def _sync_apply_explainer(self) -> str:
        """The apply channel's explainer carries the Start button (self.start_view)."""
        channel_id = config.GEAR_CHECK_CHANNEL_ID or None
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        return await bot_messages.sync_pinned(channel, "gear_check_explainer", view=self.start_view)

    # --- archive -------------------------------------------------------------------------------------

    async def _post_or_update_archive_confirmation(self, guild, initiator: str, moved, failed):
        """One confirmation message in the review channel, edited in place on
        later runs, so the latest archive run is always the one shown."""
        review_channel = self.bot.get_channel(config.REVIEW_CHANNEL_ID)
        if review_channel is None:
            return
        archive_link = f"https://discord.com/channels/{guild.id}/{config.ARCHIVE_CHANNEL_ID}"
        content = (
            f"📦 **Archiving complete** - {moved} application(s) moved"
            + (f", {failed} failed (see the System log)" if failed else "")
            + f"\nBy {initiator} at {self._timestamp()}\n"
            + f"Archived applications: {archive_link}"
        )
        record = self.bot.store.get(ARCHIVE_CONFIRMATION_KEY)
        if record:
            try:
                message = await review_channel.fetch_message(record["message_id"])
                await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                return
            except (discord.NotFound, discord.Forbidden):
                pass  # the old confirmation is gone: post a new one
        message = await review_channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())
        self.bot.store.set(ARCHIVE_CONFIRMATION_KEY, message_id=message.id)

    def archive_problem(self) -> Optional[str]:
        """Why archiving can't run now, or None."""
        if not config.ARCHIVE_ENABLED or not config.ARCHIVE_CHANNEL_ID:
            return ("Archiving isn't set up: turn it on and pick the archive channel in Settings (Features, "
                    "Applications).")
        if self.bot.get_channel(int(config.ARCHIVE_CHANNEL_ID)) is None:
            return "The bot can't see the archive channel. Check the channel and the bot's permissions."
        if self.bot.get_channel(config.REVIEW_CHANNEL_ID) is None:
            return "The bot can't see the review channel."
        return None

    async def archive_decided(self, initiator: str) -> Tuple[int, int]:
        """Moves every decided card from the review channel to the archive
        channel (re-posted there, original deleted). (moved, failed)."""
        problem = self.archive_problem()
        if problem:
            raise ApplicationError(problem)
        archive_channel = self.bot.get_channel(int(config.ARCHIVE_CHANNEL_ID))
        review_channel = self.bot.get_channel(config.REVIEW_CHANNEL_ID)
        moved = failed = 0
        # Oldest first, so the archive keeps the review channel's order.
        async for message in review_channel.history(limit=None, oldest_first=True):
            application = self.bot.store.get(message.id)
            if not applications.is_application(application) or application.get("status") not in ("approved", "denied"):
                continue
            try:
                new_message = await archive_channel.send(
                    view=self._render_application_view(application, message_id=message.id))
                self.bot.store.set(new_message.id, **{
                    **application, "channel_id": archive_channel.id,
                    "original_message_id": application.get("original_message_id") or message.id,
                    "submitted_at": applications.submitted_at(message.id, application)})
                self.bot.store.delete(message.id)
                await message.delete()
                applications.changed(new_message.id)
                moved += 1
                await asyncio.sleep(0.5)  # stay clear of rate limits on big batches
            except Exception:
                log.exception("Failed to archive application message %s", message.id)
                failed += 1
        await self._post_or_update_archive_confirmation(review_channel.guild, initiator, moved, failed)
        return moved, failed

    @applications_group.command(
        name="archive", description="Move all approved/denied applications to the archive channel"
    )
    async def applications_archive(self, interaction: discord.Interaction):
        if not await member_is_moderator(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can archive applications.", ephemeral=True)
            return
        problem = self.archive_problem()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            moved, failed = await self.archive_decided(interaction.user.mention)
        except ApplicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        summary = f"Archived {moved} application(s)."
        if failed:
            summary += f" {failed} failed - see the System log."
        await interaction.followup.send(summary, ephemeral=True)

    # --- /lookup -------------------------------------------------------------------------------------

    @app_commands.command(name="lookup", description="Look up any character on Blizzard (moderators)")
    @app_commands.describe(name="The character's name and surname, e.g. Eggmike Thewhite",
                           public="Post it in this channel instead of only showing it to you")
    async def lookup(self, interaction: discord.Interaction, name: str, public: bool = False):
        if not is_moderator(interaction.user):
            await interaction.response.send_message("Only moderators can look up characters.", ephemeral=True)
            return
        if getattr(self.bot, "blizzard", None) is None:
            await interaction.response.send_message(
                "Blizzard API keys aren't set up (Settings, General, Integrations).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=not public, thinking=True)
        class_names, _ = await self._reference()
        try:
            summary = await gear.fetch(self.bot, wow_api.slug(name), class_names)
        except (gear.GearError, wow_api.WowApiError) as exc:
            await interaction.followup.send(f"Couldn't look up **{name.strip()}**: {exc}", ephemeral=True)
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


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplyCog(bot))
