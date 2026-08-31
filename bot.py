"""
Entry point. Loads feature cogs (currently just guild applications) and
starts the bot. Adding a new, unrelated feature later = add a new file to
cogs/ and one line in EXTENSIONS below - this file shouldn't need much else
touched.
"""

import os
import asyncio
import logging
import time

import discord
from discord.ext import commands
from dotenv import load_dotenv

from wcl_client import WarcraftLogsClient
from wowhead import WowheadClient
from blizzard_client import BlizzardClient
from storage import ApplicationStore
import icons

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wow-apply-bot")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# Feature cogs to load. Add a new entry here when you add a new feature file.
EXTENSIONS = [
    "cogs.apply",
    "cogs.announcements",
    "cogs.emoji_admin",
    "cogs.attendance",
    "cogs.raid_summary",
    "cogs.raid_logs",
    "cogs.tier_retrospective",
]

intents = discord.Intents.default()
intents.members = True          # needed to assign roles / fetch member objects
intents.reactions = True
intents.message_content = True  # needed so message.attachments is populated
                                 # for the screenshot-reply step (this is a
                                 # privileged intent - see README step below)

bot = commands.Bot(command_prefix="!", intents=intents)

# Shared singletons every cog can reach via self.bot.<name> - avoids each
# feature file re-instantiating its own WCL client / storage.
bot.wcl = WarcraftLogsClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"])
bot.wowhead = WowheadClient()
# Optional - item lookups (loot names/icons, potion icons) prefer this over
# wowhead.py's scraped XML feed when configured (see
# cogs/raid_summary.py's _get_item_data) since it's Blizzard's own
# sanctioned API with no anti-bot/IP-block concerns, but nothing requires
# it: unset BLIZZARD_CLIENT_ID/SECRET just means those lookups keep going
# through bot.wowhead exactly as before this existed. Same
# SERVER_REGION env var wcl_client.py already reads (defaults "us").
blizzard_client_id = os.environ.get("BLIZZARD_CLIENT_ID")
blizzard_client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET")
bot.blizzard = (
    BlizzardClient(blizzard_client_id, blizzard_client_secret, region=os.environ.get("SERVER_REGION", "us"))
    if blizzard_client_id and blizzard_client_secret else None
)
bot.store = ApplicationStore()


@bot.event
async def on_ready():
    startup_start = time.monotonic()
    log.info("=== Startup: connected to Discord, running post-connect setup now ===")

    log.info("Startup: provisioning application emoji (class/role/spec icons)...")
    step_start = time.monotonic()
    await icons.provision_app_emojis(bot)
    log.info("Startup: emoji provisioning done (%.1fs)", time.monotonic() - step_start)

    log.info("Startup: syncing slash commands...")
    step_start = time.monotonic()
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if guild_id:
        # Guild-scoped sync propagates in seconds instead of global sync's
        # up-to-an-hour delay. copy_global_to() first copies every
        # globally-defined command into this guild's command list, so
        # commands can still be written normally (no guild= on each one)
        # and just get synced here - this bot only ever runs in one guild
        # anyway, so there's no downside to always doing this when set.
        guild = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("Synced %d command(s) to guild %s: %s", len(synced), guild_id, [c.name for c in synced])

        # Remove any GLOBAL registrations left over from before guild-scoped
        # sync was introduced (or from testing without DISCORD_GUILD_ID set).
        # Without this, Discord ends up with two separate registrations of
        # the same command - the guild-scoped one (works, instant) and a
        # stale global one (can show up as a duplicate entry that doesn't
        # actually invoke anything). Safe to run every startup - clearing
        # an already-empty global set is a harmless no-op.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    else:
        synced = await bot.tree.sync()
        log.info(
            "Synced %d command(s) GLOBALLY (up to ~1hr to propagate - set "
            "DISCORD_GUILD_ID in .env for instant sync instead): %s",
            len(synced), [c.name for c in synced],
        )
    log.info("Startup: command sync done (%.1fs)", time.monotonic() - step_start)

    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    log.info(
        "=== Startup: bot.py setup complete (%.1fs so far) - each cog's own "
        "on_ready (pinned messages, roster refresh, etc.) runs separately and "
        "may still be in progress; watch for its own 'startup complete' line ===",
        time.monotonic() - startup_start,
    )


async def main():
    async with bot:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        try:
            await bot.start(DISCORD_TOKEN)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("Shutting down gracefully...")
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # suppress the final traceback from asyncio.run()
        print("Bot stopped.")