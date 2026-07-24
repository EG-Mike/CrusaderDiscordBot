"""
Announcements feature.

Flow:
  1. A moderator runs /announce.
  2. A modal collects the announcement text (title optional, body required).
  3. The bot posts a DRAFT to a sandbox channel (ANNOUNCEMENT_SANDBOX_CHANNEL_ID)
     with Edit/Publish buttons. This is the closest thing to WYSIWYG that
     Discord's bot API allows - there's no rich-text/live-preview input
     surface for modals, so instead you iterate on the actual rendered
     message directly: click Edit, get the same modal pre-filled, resubmit,
     see the real result immediately. Repeat as many times as needed - the
     draft persists (other mods can see it too), it's not tied to one
     ephemeral interaction that expires.
  4. When ready, click Publish - a native channel picker appears, and the
     announcement gets reconstructed in its final form in the chosen
     channel. The draft in the sandbox is marked "Published" and its
     buttons removed.
  5. Every published announcement also carries its own persistent Edit
     button - ANY moderator can tweak it further after the fact, same
     pattern as the draft.

Self-contained like cogs/apply.py - a future feature is a new cog file, not
changes here.
"""

import os
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.announcements")


class AnnouncementModal(discord.ui.Modal, title="Announcement"):
    announcement_title = discord.ui.TextInput(
        label="Title (optional)",
        required=False,
        max_length=256,
    )
    body = discord.ui.TextInput(
        label="Announcement text",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=3500,
        placeholder="## Big header 🎉 works directly. A line with just --- starts a new section.",
    )

    def __init__(
        self,
        cog: "AnnouncementsCog",
        *,
        mode: str = "create",  # "create" | "edit_draft" | "edit_published"
        message_id=None,
        prefill_title=None,
        prefill_body=None,
    ):
        super().__init__()
        self.cog = cog
        self.mode = mode
        self.message_id = message_id
        if prefill_title:
            self.announcement_title.default = prefill_title
        if prefill_body:
            self.body.default = prefill_body

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        title = str(self.announcement_title).strip() or None
        body = str(self.body).strip()

        if self.mode == "create":
            await self.cog._create_draft(interaction, title, body)
        elif self.mode == "edit_draft":
            await self.cog._apply_draft_edit(interaction, self.message_id, title, body)
        elif self.mode == "edit_published":
            await self.cog._apply_published_edit(interaction, self.message_id, title, body)


class AnnouncementsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.sandbox_channel_id = os.environ.get("ANNOUNCEMENT_SANDBOX_CHANNEL_ID")
        # Separate JSON file from applications.json, but the same small
        # generic store mechanism - see storage.py.
        self.store = ApplicationStore(path="announcements.json")
        self.bot.announcement_store = self.store

    async def cog_load(self):
        # Registers the persistent buttons so they keep working across bot
        # restarts, on every previously-posted draft/announcement.
        self.bot.add_view(self._render_draft({"title": None, "body": ""}))
        self.bot.add_view(self._render_posted_announcement({"title": None, "body": ""}))

    # --- permission check ------------------------------------------------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    # --- rendering ---------------------------------------------------------

    def _split_into_sections(self, body: str) -> list:
        """Splits on any line that is exactly '---' (surrounding whitespace
        ignored) into separate chunks - each becomes its own TextDisplay,
        joined by a real Separator() component. Headers (## Text) and
        emoji need no special handling here - TextDisplay already follows
        normal Discord message markdown, so they just work as typed."""
        sections = []
        current = []
        for line in body.split("\n"):
            if line.strip() == "---":
                sections.append("\n".join(current).strip())
                current = []
            else:
                current.append(line)
        sections.append("\n".join(current).strip())
        return [s for s in sections if s]

    def _build_announcement_container(self, record: dict) -> discord.ui.Container:
        container = discord.ui.Container(accent_color=discord.Color.blurple())
        if record.get("title"):
            container.add_item(discord.ui.TextDisplay(f"## {record['title']}"))
            container.add_item(discord.ui.Separator())

        sections = self._split_into_sections(record.get("body") or "")
        if not sections:
            sections = [""]
        for i, section in enumerate(sections):
            container.add_item(discord.ui.TextDisplay(section))
            if i < len(sections) - 1:
                container.add_item(discord.ui.Separator())

        return container

    def _render_draft(self, record: dict) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(self._build_announcement_container(record))
        view.add_item(discord.ui.TextDisplay("-# 📝 Draft - not yet published"))

        action_row = discord.ui.ActionRow()
        edit_button = discord.ui.Button(
            label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id="wow_announce_draft_edit"
        )
        edit_button.callback = self._on_draft_edit_click
        publish_button = discord.ui.Button(
            label="🚀 Publish", style=discord.ButtonStyle.success, custom_id="wow_announce_draft_publish"
        )
        publish_button.callback = self._on_draft_publish_click
        action_row.add_item(edit_button)
        action_row.add_item(publish_button)
        view.add_item(action_row)
        return view

    def _render_posted_announcement(self, record: dict) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(self._build_announcement_container(record))

        action_row = discord.ui.ActionRow()
        edit_button = discord.ui.Button(
            label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id="wow_announce_edit"
        )
        edit_button.callback = self._on_published_edit_click
        action_row.add_item(edit_button)
        view.add_item(action_row)
        return view

    # --- /announce command: creates a draft --------------------------------

    @app_commands.command(name="announce", description="Draft an announcement (moderator only)")
    async def announce(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only moderators can post announcements.", ephemeral=True
            )
            return
        await interaction.response.send_modal(AnnouncementModal(self, mode="create"))

    async def _create_draft(self, interaction: discord.Interaction, title, body):
        if not self.sandbox_channel_id:
            await interaction.followup.send(
                "No sandbox channel is configured - set ANNOUNCEMENT_SANDBOX_CHANNEL_ID "
                "in the bot's .env first.",
                ephemeral=True,
            )
            return
        channel = self.bot.get_channel(int(self.sandbox_channel_id))
        if channel is None:
            await interaction.followup.send(
                "Couldn't find the configured sandbox channel - check "
                "ANNOUNCEMENT_SANDBOX_CHANNEL_ID and that the bot can see it.",
                ephemeral=True,
            )
            return

        record = {"title": title, "body": body}
        message = await channel.send(view=self._render_draft(record))
        self.store.set(message.id, title=title, body=body, is_draft=True, published=False)

        await interaction.followup.send(
            f"Draft posted in {channel.mention} - tweak it there with Edit, then hit "
            "Publish when it's ready.",
            ephemeral=True,
        )

    # --- draft editing -------------------------------------------------

    async def _on_draft_edit_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only moderators can edit drafts.", ephemeral=True
            )
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this draft's record.", ephemeral=True
            )
            return
        modal = AnnouncementModal(
            self,
            mode="edit_draft",
            message_id=interaction.message.id,
            prefill_title=record.get("title"),
            prefill_body=record.get("body"),
        )
        await interaction.response.send_modal(modal)

    async def _apply_draft_edit(self, interaction: discord.Interaction, message_id: int, title, body):
        record = self.store.get(message_id)
        if record is None:
            await interaction.followup.send(
                "Couldn't find this draft's record - it may have been published or removed.",
                ephemeral=True,
            )
            return

        self.store.update(message_id, title=title, body=body)
        record = self.store.get(message_id)

        channel = self.bot.get_channel(int(self.sandbox_channel_id))
        if channel is None:
            await interaction.followup.send("Couldn't find the sandbox channel.", ephemeral=True)
            return
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send(
                "Couldn't find the draft message to update - it may have been deleted.",
                ephemeral=True,
            )
            return

        await message.edit(view=self._render_draft(record))
        await interaction.followup.send("Draft updated - check the sandbox channel.", ephemeral=True)

    # --- publishing ------------------------------------------------------

    async def _on_draft_publish_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only moderators can publish announcements.", ephemeral=True
            )
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this draft's record.", ephemeral=True
            )
            return
        if record.get("published"):
            await interaction.response.send_message(
                "This draft has already been published.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._start_publish_channel_selection(interaction, interaction.message.id, record)

    async def _start_publish_channel_selection(
        self, interaction: discord.Interaction, draft_message_id: int, record: dict
    ):
        channel_select = discord.ui.ChannelSelect(
            placeholder="Choose a channel to publish this announcement to",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )
        picked = asyncio.Event()
        chosen = {}

        async def _on_select(select_interaction: discord.Interaction):
            if select_interaction.user.id != interaction.user.id:
                await select_interaction.response.send_message(
                    "This isn't your draft to publish.", ephemeral=True
                )
                return
            chosen["channel_id"] = channel_select.values[0].id
            await select_interaction.response.defer()
            picked.set()

        channel_select.callback = _on_select
        view = discord.ui.View(timeout=300)
        view.add_item(channel_select)
        await interaction.followup.send("Publish to which channel?", view=view, ephemeral=True)

        try:
            await asyncio.wait_for(picked.wait(), timeout=310)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Timed out selecting a channel - click Publish again.", ephemeral=True
            )
            return

        target_channel = self.bot.get_channel(chosen["channel_id"])
        if target_channel is None:
            await interaction.followup.send("Couldn't find that channel.", ephemeral=True)
            return

        final_record = {"title": record.get("title"), "body": record.get("body")}
        message = await target_channel.send(view=self._render_posted_announcement(final_record))
        self.store.set(
            message.id,
            title=final_record["title"],
            body=final_record["body"],
            channel_id=chosen["channel_id"],
            created_by=interaction.user.id,
            is_draft=False,
        )

        # Mark the draft as published and strip its buttons so it can't be
        # re-published by accident.
        self.store.update(
            draft_message_id, published=True, published_channel_id=chosen["channel_id"]
        )
        sandbox_channel = self.bot.get_channel(int(self.sandbox_channel_id))
        if sandbox_channel is not None:
            try:
                draft_message = await sandbox_channel.fetch_message(draft_message_id)
                published_marker_view = discord.ui.LayoutView(timeout=None)
                published_marker_view.add_item(self._build_announcement_container(record))
                published_marker_view.add_item(discord.ui.TextDisplay(
                    f"-# ✅ Published to <#{chosen['channel_id']}> by {interaction.user}"
                ))
                await draft_message.edit(view=published_marker_view)
            except (discord.NotFound, discord.Forbidden):
                pass

        await interaction.followup.send(f"🚀 Published to <#{chosen['channel_id']}>.", ephemeral=True)

    # --- editing an already-published announcement --------------------

    async def _on_published_edit_click(self, interaction: discord.Interaction):
        if not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message(
                "Only moderators can edit announcements.", ephemeral=True
            )
            return
        record = self.store.get(interaction.message.id)
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this announcement's record.", ephemeral=True
            )
            return
        modal = AnnouncementModal(
            self,
            mode="edit_published",
            message_id=interaction.message.id,
            prefill_title=record.get("title"),
            prefill_body=record.get("body"),
        )
        await interaction.response.send_modal(modal)

    async def _apply_published_edit(self, interaction: discord.Interaction, message_id: int, title, body):
        record = self.store.get(message_id)
        if record is None:
            await interaction.followup.send(
                "Couldn't find this announcement's record - it may have been removed.",
                ephemeral=True,
            )
            return

        self.store.update(message_id, title=title, body=body)
        record = self.store.get(message_id)

        channel = self.bot.get_channel(record.get("channel_id"))
        if channel is None:
            await interaction.followup.send(
                "Couldn't find the channel this announcement was posted in.", ephemeral=True
            )
            return

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send(
                "Couldn't find the announcement message to update - it may have been deleted.",
                ephemeral=True,
            )
            return

        await message.edit(view=self._render_posted_announcement(record))
        await interaction.followup.send("Announcement updated!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnnouncementsCog(bot))