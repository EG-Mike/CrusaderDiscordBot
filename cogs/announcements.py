"""
Announcements, Discord side. The data and the Discord rendering live in
announcements.py, shared with the dashboard's announcement builder; this cog
is the /announce flow:

  1. A moderator runs /announce; a modal collects a title (optional) and
     the text.
  2. The bot posts a DRAFT in the sandbox channel
     (ANNOUNCEMENT_SANDBOX_CHANNEL_ID) with Edit and Publish buttons, so you
     iterate on the real rendered message. The same draft also shows up
     on the dashboard, where it can be edited with a live preview.
  3. Publish asks for a channel and posts the final message there. The
     sandbox draft then shows "Published to #channel" and its Edit button
     edits the published message. The public message has no buttons.

Every button finds its announcement by the message it's on (the sandbox
draft or, for announcements published before the sandbox Edit button
existed, the published message itself), so messages posted by earlier
versions of the bot keep working.

On first start, announcements stored by earlier versions (the
announcements.json store) are imported into the database once; the old
store is left untouched as a backup.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import announcements as service
import config
from permissions import member_is_moderator
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.announcements")

LEGACY_PUBLISHED_EDIT_ID = "wow_announce_edit"  # Edit button on very old published messages


def _actor(user: discord.abc.User) -> str:
    return f"{getattr(user, 'display_name', None) or user.name} ({user.id})"


class AnnouncementModal(discord.ui.Modal, title="Announcement"):
    announcement_title = discord.ui.TextInput(label="Title (optional)", required=False, max_length=service.MAX_TITLE)
    body = discord.ui.TextInput(
        label="Announcement text",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=3800,
        placeholder="## Big header 🎉 works directly. A line with just --- starts a new section.",
    )

    def __init__(self, cog: "AnnouncementsCog", *, mode: str, announcement: dict = None):
        """mode: "create" | "edit_draft" | "edit_published"."""
        super().__init__()
        self.cog = cog
        self.mode = mode
        self.announcement = announcement
        if announcement:
            self.announcement_title.default = announcement.get("title")
            self.body.default = announcement.get("body")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        fields = {"title": str(self.announcement_title), "body": str(self.body)}
        try:
            message = await self.cog.save_from_modal(interaction, self.mode, self.announcement, fields)
        except service.AnnouncementError as exc:
            message = f"⚠️ {exc}"
        await interaction.followup.send(message, ephemeral=True)


class AnnouncementsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        try:
            imported = await asyncio.to_thread(
                service.import_legacy, ApplicationStore(path="announcements.json").items()
            )
            if imported:
                log.info("Announcements: imported %d earlier announcement(s) into the database", imported)
        except Exception:
            log.exception("Announcements: importing earlier announcements failed - they won't show on the dashboard")

        # Persistent buttons: keep working across restarts on every message
        # that carries them, whichever process posted it.
        service.set_button_handlers({
            service.DRAFT_EDIT_ID: self._on_draft_edit,
            service.DRAFT_PUBLISH_ID: self._on_draft_publish,
            service.EDIT_PUBLISHED_ID: self._on_edit_published,
        })
        placeholder = {"title": None, "body": "", "channel_id": None}
        self.bot.add_view(service.draft_view(placeholder))
        self.bot.add_view(service.published_marker_view(placeholder))
        legacy = discord.ui.View(timeout=None)
        button = discord.ui.Button(label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id=LEGACY_PUBLISHED_EDIT_ID)
        button.callback = self._on_edit_published
        legacy.add_item(button)
        self.bot.add_view(legacy)

    # --- permission check ------------------------------------------------


    async def _refuse_non_mod(self, interaction: discord.Interaction, what: str) -> bool:
        if await member_is_moderator(interaction.guild, interaction.user.id):
            return False
        await interaction.response.send_message(f"Only moderators can {what}.", ephemeral=True)
        return True

    def _sandbox(self):
        channel_id = config.ANNOUNCEMENT_SANDBOX_CHANNEL_ID
        return self.bot.get_channel(int(channel_id)) if channel_id else None

    # --- /announce -----------------------------------------------------------

    @app_commands.command(name="announce", description="Draft an announcement (moderator only)")
    async def announce(self, interaction: discord.Interaction):
        if await self._refuse_non_mod(interaction, "post announcements"):
            return
        await interaction.response.send_modal(AnnouncementModal(self, mode="create"))

    async def save_from_modal(self, interaction: discord.Interaction, mode: str, announcement, fields: dict) -> str:
        """Applies a submitted modal; returns the reply for the moderator."""
        by = _actor(interaction.user)
        if mode == "create":
            sandbox = self._sandbox()
            if sandbox is None:
                return ("No announcement drafts channel is set up (or the bot can't see it). Set it in the "
                        "dashboard's Settings, or write the announcement on the dashboard instead.")
            service.check_publishable(service.clean_fields(fields))
            record = service.create(fields, by)
            message = await sandbox.send(view=service.draft_view(record))
            service.set_sandbox_message(record["id"], message.id)
            return (f"Draft posted in {sandbox.mention}. Tweak it there with Edit (or on the dashboard), "
                    "then hit Publish when it's ready.")

        record = service.get(announcement["id"])
        if mode == "edit_published":
            await service.edit_published(self.bot, record["id"], fields, by)
            return "Announcement updated."
        service.check_publishable({**record, **service.clean_fields(fields)})
        record = service.update(record["id"], fields, by)
        await service.refresh_sandbox(self.bot, record)
        return "Draft updated - check the sandbox channel."

    # --- buttons --------------------------------------------------------------

    async def _on_draft_edit(self, interaction: discord.Interaction):
        if await self._refuse_non_mod(interaction, "edit drafts"):
            return
        record = service.find_by_message(interaction.message.id)
        if record is None:
            await interaction.response.send_message("Couldn't find this draft any more.", ephemeral=True)
            return
        mode = "edit_published" if record["status"] == "published" else "edit_draft"
        await interaction.response.send_modal(AnnouncementModal(self, mode=mode, announcement=record))

    async def _on_edit_published(self, interaction: discord.Interaction):
        if await self._refuse_non_mod(interaction, "edit announcements"):
            return
        record = service.find_by_message(interaction.message.id)
        if record is None or record["status"] != "published":
            await interaction.response.send_message("Couldn't find the published announcement.", ephemeral=True)
            return
        await interaction.response.send_modal(AnnouncementModal(self, mode="edit_published", announcement=record))

    async def _on_draft_publish(self, interaction: discord.Interaction):
        if await self._refuse_non_mod(interaction, "publish announcements"):
            return
        record = service.find_by_message(interaction.message.id)
        if record is None:
            await interaction.response.send_message("Couldn't find this draft any more.", ephemeral=True)
            return
        if record["status"] == "published":
            await interaction.response.send_message("This draft has already been published.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        select = discord.ui.ChannelSelect(
            placeholder="Choose a channel to publish this announcement to",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )
        picked = asyncio.Event()
        chosen = {}

        async def on_select(select_interaction: discord.Interaction):
            if select_interaction.user.id != interaction.user.id:
                await select_interaction.response.send_message("This isn't your draft to publish.", ephemeral=True)
                return
            chosen["channel_id"] = select.values[0].id
            await select_interaction.response.defer()
            picked.set()

        select.callback = on_select
        view = discord.ui.View(timeout=300)
        view.add_item(select)
        await interaction.followup.send("Publish to which channel?", view=view, ephemeral=True)
        try:
            await asyncio.wait_for(picked.wait(), timeout=310)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out choosing a channel - click Publish again.", ephemeral=True)
            return
        try:
            await service.publish(self.bot, record["id"], chosen["channel_id"], _actor(interaction.user))
        except service.AnnouncementError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"🚀 Published to <#{chosen['channel_id']}>.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnnouncementsCog(bot))
