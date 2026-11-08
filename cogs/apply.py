"""
Guild applications.

Flow:
  1. The applicant clicks "Start application" in the apply channel (or
     uses /apply) and types their character's name and surname in one
     field. The bot reads the character from Blizzard's API (gear.py):
     class, level, guild, talents, statistics, and the equipped gear with
     enchants and gems.
  2. In the apply channel, in one message only the applicant sees
     (ApplyDialog, no DMs): the main spec is pre-selected from where the
     talent points are, with a role to match; the applicant confirms or
     changes them and can pick an offspec (optional).
  3. "Add note & screenshots" opens a form (NoteModal) with a note and a
     file upload; "Submit application" posts it. Form uploads are only kept
     by Discord for a while, so the screenshots are re-attached to the card
     (screenshot_files; a card's own files are only listed in its gallery,
     card_media_urls). /update-application opens the same form.
  4. The application is posted to the review channel: the gear summary,
     the statistics that matter for the role, requirements (Settings:
     minimum level and item level), and, once WarcraftLogs covers the game
     (APPLICATION_WARCRAFTLOGS_ENABLED), the applicant's parses. It also
     shows on the dashboard's Applications page.
  5. A moderator or approver approves (roles, nickname, DM, and the character is linked
     to the applicant on the guild roster with their specs) or denies (DM),
     on the card or on the dashboard. Reset undoes a decision, "Refresh"
     reads the character from Blizzard again. The decision DMs are the only
     DMs the bot sends applicants.

Decisions are methods without a Discord interaction (approve, deny,
reset, refresh_gear, archive_decided), so the card's buttons and the
dashboard (webapi/applications_api.py) follow the same rules. Shared
record helpers are in applications.py.

Moderators can also look up any character without an application: /lookup
(the dashboard's Character lookup does the same).
"""

import asyncio
import io
import logging
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import application_card
import applications
import bot_messages
import config
import emoji_library
import gear
import icons
import roster
import wow_api
from applications import ApplicationError, Reviewer
from permissions import is_moderator, member_is_moderator
from web_links import dashboard_link

log = logging.getLogger("wow-apply-bot.apply")

FRESH_ROLE_EMOJI = "<:fresh_role:1542590697238962226>"
NO_OFFSPEC = "__none__"

DIALOG_TIMEOUT = 840       # the in-channel dialog; Discord lets the bot edit it for 15 minutes
NOT_FOUND_TIMEOUT = 180    # waiting for the retry-name/continue-anyway choice
NOTE_LIMIT = 1000          # characters in the applicant's note

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

ARCHIVE_CONFIRMATION_KEY = "archive_confirmation"  # sentinel store key, not a real message id
BLIZZARD_ROLES = {"tank": "Tank", "healer": "Healer", "dps": "DPS"}  # wow_api spec roles -> ours


def _fresh_label(name: str) -> str:
    """Prefixes the Fresh role's emoji in front of its name wherever it's
    mentioned in an assigned/already-had role list - other exempt roles
    (Regular/Organizer) are left as plain text."""
    return f"{FRESH_ROLE_EMOJI} {name}" if name == "Fresh" else name


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


def screenshot_name(index: int, original: str) -> str:
    """The file name a screenshot gets on the review card ("screenshot-1.png")."""
    ext = original.rsplit(".", 1)[-1].lower() if "." in (original or "") else "png"
    return f"screenshot-{index}.{ext if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp') else 'png'}"


def card_media_urls(message) -> List[str]:
    """The addresses of the images in a card's gallery. A card's own files
    (screenshots) aren't listed in message.attachments: Discord only gives
    their addresses inside the components that show them."""
    found: List[str] = []

    def walk(components):
        for c in components or []:
            if getattr(c, "type", None) == discord.ComponentType.media_gallery:
                found.extend(item.media.url for item in c.items if getattr(item.media, "url", None))
            walk(getattr(c, "children", None) or getattr(c, "components", None))

    walk(getattr(message, "components", None))
    return found


def image_uploads(attachments) -> list:
    """The uploaded files that are images, at most MAX_SCREENSHOTS."""
    return [a for a in attachments or [] if (a.content_type or "").startswith("image/")][: config.MAX_SCREENSHOTS]


class NoteModal(discord.ui.Modal):
    """An optional note and screenshots, as a form: used while applying (the
    dialog's "Add note & screenshots") and by /update-application. Uploads
    are only kept by Discord for a while, so the bot re-attaches them to the
    review card (ApplyCog._screenshot_files)."""

    def __init__(self, *, title: str, note: Optional[str], on_done):
        super().__init__(title=title, timeout=DIALOG_TIMEOUT)
        self.on_done = on_done
        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=NOTE_LIMIT,
                                         default=note or None,
                                         placeholder="Anything the reviewers should know, e.g. your experience")
        self.uploads = discord.ui.FileUpload(required=False, max_values=max(1, int(config.MAX_SCREENSHOTS or 1)))
        self.add_item(discord.ui.Label(text="Note (optional)", component=self.note))
        self.add_item(discord.ui.Label(text="Screenshots (optional)", component=self.uploads,
                                       description=f"Up to {config.MAX_SCREENSHOTS} images, e.g. your gear"))

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_done(interaction, str(self.note.value or "").strip() or None, image_uploads(self.uploads.values))


class ApplyDialog(discord.ui.View):
    """The whole application after the name, in one message only the
    applicant sees, in the apply channel: main spec (pre-selected from their
    talent points), role and an optional offspec, "Add note & screenshots"
    (NoteModal) and "Submit application". `specs`: the class's spec names
    for the game version; `spec_roles`: Blizzard's role per spec name (may be
    empty); `intro`: the text above the status line."""

    def __init__(self, user_id, guild, class_name, specs, spec_roles=None, suggested_spec=None,
                 suggested_role=None, intro: str = ""):
        super().__init__(timeout=DIALOG_TIMEOUT)
        self.intro = intro
        self.note: Optional[str] = None
        self.uploads: list = []
        self.submit_interaction: Optional[discord.Interaction] = None
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

        note_button = discord.ui.Button(label="Add note & screenshots", emoji="📝", style=discord.ButtonStyle.secondary,
                                        row=3)
        note_button.callback = self._on_note
        self.add_item(note_button)
        submit_button = discord.ui.Button(label="Submit application", emoji="📨", style=discord.ButtonStyle.success,
                                          row=3)
        submit_button.callback = self._on_submit
        self.add_item(submit_button)
        self._sync()

    def text(self) -> str:
        """The message: the intro, then what's attached so far."""
        attached = [f"📝 note ({len(self.note)} characters)" if self.note else None,
                    f"🖼️ {len(self.uploads)} screenshot{'s' if len(self.uploads) != 1 else ''}" if self.uploads
                    else None]
        status = " · ".join(filter(None, attached)) or "no note or screenshots (optional)"
        return f"{self.intro}\n-# Attached: {status}"

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

    async def _on_note(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return

        async def done(modal_interaction: discord.Interaction, note, uploads):
            self.note = note
            if uploads or not self.uploads:  # an empty upload field keeps the screenshots added before
                self.uploads = uploads
            await modal_interaction.response.edit_message(content=self.text(), view=self)

        await interaction.response.send_modal(NoteModal(title="Note & screenshots", note=self.note, on_done=done))

    async def _on_submit(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        if not self.role or not self.main_spec:
            await interaction.response.send_message("Please pick your main spec and role first.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"{self.text()}\n\n⏳ Posting your application…", view=self)
        self.submit_interaction = interaction
        self.done.set()
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
        }, all_buttons=True)
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

    def _render_application_view(self, application: dict, message_id: int = None, *,
                                 all_buttons: bool = False) -> discord.ui.LayoutView:
        """The review card (application_card.py), rebuilt from the stored
        record for the first post and every later edit, so exactly one code
        path knows how to draw an application. message_id: this
        application's own message, left out when counting earlier denials.
        all_buttons: every action button, for registering the persistent view."""
        return application_card.build(
            application,
            prior_denials=applications.prior_denials(self.bot.store, application["applicant_id"], exclude=message_id),
            callbacks={"approve": self._on_approve_click, "deny": self._on_deny_click, "reset": self._on_reset_click,
                       "refresh": self._on_refresh_gear_click},
            guild=self._guild(),
            dashboard_url=dashboard_link(f"/applications/{message_id}") if message_id else None,
            all_buttons=all_buttons,
        )

    async def _gear_fields(self, summary) -> dict:
        """What an application stores about the character: the summary (data)
        and the emoji of its items and professions, so a re-render never
        needs Blizzard."""
        if not summary:
            return {"gear": None, "icons": {}, "gear_checked_at": None}
        return {"gear": summary, "icons": await self._card_icons(summary), "gear_checked_at": int(time.time())}

    async def _card_icons(self, summary: dict) -> Dict[str, str]:
        """{"item:id" / "profession:id": emoji} from the emoji library, for
        the card and /lookup. Icons that can't be made are left out."""
        found = {application_card.icon_key("item", item_id): emoji
                 for item_id, emoji in (await gear.item_icons(self.bot, summary)).items() if emoji}
        for profession in summary.get("professions") or []:
            if profession.get("primary") and profession.get("id"):
                emoji = await emoji_library.ensure_icon(self.bot, "profession", profession["id"],
                                                        by="automatic (application)")
                if emoji:
                    found[application_card.icon_key("profession", profession["id"])] = emoji
        return found

    # --- where an application's card is ---------------------------------------------------------------

    def _lock(self, message_id: int) -> asyncio.Lock:
        """One decision at a time per application (two reviewers clicking at once)."""
        return self._locks.setdefault(int(message_id), asyncio.Lock())

    def _guild(self):
        get_channel = getattr(self.bot, "get_channel", None)
        channel = get_channel(config.REVIEW_CHANNEL_ID) if get_channel and config.REVIEW_CHANNEL_ID else None
        if channel is not None and getattr(channel, "guild", None) is not None:
            return channel.guild
        guilds = getattr(self.bot, "guilds", None) or []
        return guilds[0] if guilds else None

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
        self.bot.store.update(message_id, **await self._gear_fields(summary))
        await self._rerender(message_id)
        return self.bot.store.get(message_id)

    # --- Discord buttons ------------------------------------------------------------------------------

    @staticmethod
    def is_approver(member) -> bool:
        """Members with the approver role (Settings, Applications)."""
        return (isinstance(member, discord.Member) and bool(config.APPROVER_ROLE_ID)
                and any(role.id == int(config.APPROVER_ROLE_ID) for role in member.roles))

    @classmethod
    def can_decide(cls, member) -> bool:
        """Who may approve, deny, reset and refresh: moderators (permissions.py)
        and members with the approver role, so a reviewer doesn't need to be a
        moderator. Anyone else can read the review channel without acting on it."""
        return is_moderator(member) or cls.is_approver(member)

    async def _button_guard(self, interaction: discord.Interaction) -> Optional[dict]:
        application = self.bot.store.get(interaction.message.id)
        if not applications.is_application(application):
            await interaction.response.send_message("Couldn't find this application's record.", ephemeral=True)
            return None
        if not self.can_decide(interaction.user):
            await interaction.response.send_message(
                "Only moderators and approvers can act on applications.", ephemeral=True)
            return None
        return application

    async def _on_approve_click(self, interaction: discord.Interaction):
        if await self._button_guard(interaction) is None:
            return
        await interaction.response.defer()  # silent: the card itself shows the result
        try:
            await self.approve(interaction.message.id, applications.discord_reviewer(interaction.user))
        except ApplicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def _on_deny_click(self, interaction: discord.Interaction):
        application = await self._button_guard(interaction)
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
        if await self._button_guard(interaction) is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.reset(interaction.message.id, applications.discord_reviewer(interaction.user))
        except ApplicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Application reset - Approve and Deny work again.", ephemeral=True)

    async def _on_refresh_gear_click(self, interaction: discord.Interaction):
        if await self._button_guard(interaction) is None:
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
        """The rest of the application in one message only the applicant
        sees, here in the channel (ApplyDialog): main spec, role, offspec, an
        optional note and screenshots, then Submit."""
        _, class_specs = await self._reference()
        spec_objects = class_specs["by_name"].get(char_data["class_name"]) or [
            {"id": None, "name": s, "role": None} for s in config.CLASS_SPECS.get(char_data["class_name"], [])]
        specs = [s["name"] for s in spec_objects]
        spec_roles = {s["name"]: s.get("role") for s in spec_objects}
        suggested_spec = await self._suggest_spec(char_data, specs)

        summary = char_data.get("gear") or {}
        found = f"**{char_data['name']}**"
        if summary:
            found += f", level {summary.get('level') or '?'} {char_data['class_name']}"
            if summary.get("guild"):
                found += f" of <{summary['guild']}>"
        intro = (f"Found {found}! "
                 + (f"Your talent points say **{suggested_spec}**, so that's selected as your main spec.\n"
                    if suggested_spec else "\n")
                 + bot_messages.text("apply_dialog"))
        view = ApplyDialog(interaction.user.id, interaction.guild, char_data["class_name"], specs, spec_roles,
                           suggested_spec=suggested_spec,
                           suggested_role=default_role(char_data["class_name"], suggested_spec, spec_roles),
                           intro=intro)
        dialog = await interaction.followup.send(view.text(), view=view, ephemeral=True, wait=True)

        try:
            await asyncio.wait_for(view.done.wait(), timeout=DIALOG_TIMEOUT + 10)
        except asyncio.TimeoutError:
            try:
                await dialog.edit(content="⏱️ This application timed out. Press **Start application** to begin again.",
                                  view=None)
            except discord.HTTPException:
                pass
            return
        ids = {s["name"]: s.get("id") for s in spec_objects}
        choice = {"role": view.role, "spec": view.main_spec, "spec_id": ids.get(view.main_spec),
                  "offspec": view.offspec, "off_spec_id": ids.get(view.offspec) if view.offspec else None}
        result = await self._post_application(interaction, char_data, choice, view.note, view.uploads)
        try:
            await view.submit_interaction.edit_original_response(content=result, view=None)
        except discord.HTTPException:
            await interaction.followup.send(result, ephemeral=True)

    async def _screenshot_files(self, uploads, guild) -> Tuple[List[discord.File], Optional[str]]:
        """The uploaded screenshots as files for the review card (uploads are
        only kept by Discord for a while), within the server's upload limit.
        (files, a line about the ones left out, or None)."""
        limit = getattr(guild, "filesize_limit", None) or 10 * 1024 * 1024
        files, total, left_out = [], 0, 0
        for index, upload in enumerate(uploads, 1):
            if total + (upload.size or 0) > limit:
                left_out += 1
                continue
            try:
                data = await upload.read()
            except discord.HTTPException:
                log.warning("Couldn't download an uploaded screenshot", exc_info=True)
                left_out += 1
                continue
            total += len(data)
            files.append(discord.File(io.BytesIO(data), filename=screenshot_name(index, upload.filename)))
        problem = (f"{left_out} screenshot{'s were' if left_out != 1 else ' was'} too big or couldn't be read, so "
                   "it's not on your application." if left_out else None)
        return files, problem

    async def _card_files(self, message, application: dict) -> List[discord.File]:
        """A card's screenshot files (to post them again, e.g. when archiving),
        downloaded from its gallery, under the names the card refers to."""
        names = application.get("screenshot_files") or []
        files = []
        for name, url in zip(names, card_media_urls(message)):
            try:
                files.append(discord.File(io.BytesIO(await self.bot.http.get_from_cdn(url)), filename=name))
            except discord.HTTPException:
                log.warning("Couldn't copy screenshot %s of card %s", name, message.id, exc_info=True)
        return files

    async def _post_application(self, interaction, char_data, choice: dict, note, uploads) -> str:
        """Posts the card in the review channel. Returns the text for the applicant."""
        review_channel = self.bot.get_channel(config.REVIEW_CHANNEL_ID)
        if review_channel is None:
            log.error("Can't post %s's application: the review channel isn't set to a channel the bot can see",
                      char_data["name"])
            return ("Sorry, applications can't be submitted right now (the review channel isn't set up). Please let "
                    "a moderator know.")
        offspec = choice.get("offspec")
        tier_block = boss_block = hidden_block = None
        if config.APPLICATION_WARCRAFTLOGS_ENABLED and char_data.get("id"):
            tier_block, boss_block, hidden_block = await self._compute_tier_and_boss_blocks(char_data)
        files, problem = await self._screenshot_files(uploads, review_channel.guild)

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
            "screenshot_files": [f.filename for f in files],
            "screenshot_urls": [],
            "channel_id": config.REVIEW_CHANNEL_ID,
            "submitted_at": int(time.time()),
            **await self._gear_fields(char_data.get("gear")),
        }
        message = await review_channel.send(view=self._render_application_view(application), files=files)
        application["screenshot_urls"] = card_media_urls(message) if files else []  # for the dashboard
        fields = {k: v for k, v in application.items() if k not in ("applicant_id", "character_name")}
        self.bot.store.add(message.id, application["applicant_id"], application["character_name"], **fields)
        applications.changed(message.id)
        await self._rerender(message.id, message)  # adds the "Open on dashboard" link, which needs the message id
        text = bot_messages.text("application_submitted", character=application["character_name"])
        return f"{text}\n-# ⚠️ {problem}" if problem else text

    # --- /update-application ---------------------------------------------------------------------------
    # The same form as while applying, from wherever the applicant types it.

    @app_commands.command(
        name="update-application",
        description="Add or change the note/screenshot(s) on your pending application",
    )
    async def update_application(self, interaction: discord.Interaction):
        mine = [(mid, rec) for mid, rec in applications.records(self.bot.store)
                if rec.get("applicant_id") == interaction.user.id]
        if not mine:
            await interaction.response.send_message(
                "You don't have an application yet - use the Start application button or /apply first.",
                ephemeral=True)
            return
        message_id, application = mine[0]  # newest
        if application["status"] != "pending":
            await interaction.response.send_message(
                f"Your application has already been **{application['status']}** and can't be updated this way. "
                "If you'd like to be reconsidered, please reach out to an officer.", ephemeral=True)
            return

        async def done(modal_interaction: discord.Interaction, note, uploads):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await modal_interaction.followup.send(await self._update_note(message_id, note, uploads), ephemeral=True)

        await interaction.response.send_modal(
            NoteModal(title="Update your application", note=application.get("note"), on_done=done))

    async def _update_note(self, message_id: int, note: Optional[str], uploads) -> str:
        """Saves a new note (empty: removed) and, when files were uploaded,
        replaces the screenshots on the card. Returns the text for the applicant."""
        application = self.bot.store.get(message_id)
        if application is None or application["status"] != "pending":
            return "Your application was decided in the meantime, so this update wasn't applied."
        message = await self._card_message(message_id, application)
        if message is None:
            return "Couldn't find your application's card to update - please let a moderator know."
        changes, problem = {"note": note}, None
        files = []
        if uploads:
            files, problem = await self._screenshot_files(uploads, message.guild)
            changes["screenshot_files"] = [f.filename for f in files]
        self.bot.store.update(message_id, **changes)
        record = self.bot.store.get(message_id)
        view = self._render_application_view(record, message_id=message_id)
        try:
            edited = await (message.edit(view=view, attachments=files) if uploads else message.edit(view=view))
        except discord.HTTPException:
            log.warning("Couldn't update the review card %s", message_id, exc_info=True)
            return "Couldn't update your application's card - please try again in a minute."
        if uploads:
            self.bot.store.update(message_id, screenshot_urls=card_media_urls(edited))
        applications.changed(message_id)
        parts = ["note" if note else "note removed"] + ([f"{len(files)} screenshot(s)"] if uploads else [])
        text = f"✅ Updated your application: {', '.join(parts)}."
        return f"{text}\n-# ⚠️ {problem}" if problem else text

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
                # The card's screenshots are its own files: they move along.
                files = await self._card_files(message, application)
                new_message = await archive_channel.send(
                    view=self._render_application_view(application, message_id=message.id), files=files)
                self.bot.store.set(new_message.id, **{
                    **application, "channel_id": archive_channel.id,
                    "original_message_id": application.get("original_message_id") or message.id,
                    "submitted_at": applications.submitted_at(message.id, application),
                    **({"screenshot_urls": card_media_urls(new_message)} if files else {})})
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
        view = application_card.lookup_view(
            summary, icons_by_key=await self._card_icons(summary), profile_url=self._profile_url(summary["slug"]),
            guild=interaction.guild, extra_facts=await self._roster_facts(summary["slug"]),
            dashboard_url=dashboard_link(f"/lookup?name={quote(summary['character'])}"))
        await interaction.followup.send(view=view, ephemeral=not public,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def _roster_facts(self, character_slug: str) -> List[str]:
        """Where the character stands on the guild roster, as on the
        dashboard's Character lookup (nothing when the roster is off)."""
        if self.bot.get_cog("RosterCog") is None:
            return []
        c = await asyncio.to_thread(roster.get, character_slug)
        if c is None:
            return ["**Guild roster** not on it"]
        if not c["discord_id"]:
            return ["**Guild roster** on it, not linked to anyone"]
        return [f"**Guild roster** <@{c['discord_id']}> · {'main' if c['is_main'] else 'alt'}"]


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplyCog(bot))
