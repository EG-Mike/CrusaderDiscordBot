"""
Entry point. Loads feature cogs (gated per-deployment by config/deployment.py's
FEATURE_*_ENABLED flags) and starts the bot. Adding a new, unrelated
feature later = add a new file to cogs/, a FEATURE_*_ENABLED flag in
config/deployment.py, and one line in OPTIONAL_EXTENSIONS below - this file
shouldn't need much else touched.
"""

import os
import asyncio
import logging
import time

import discord
from discord.ext import commands
from dotenv import load_dotenv

import config
from wcl_client import WarcraftLogsClient
from wowhead import WowheadClient
from blizzard_client import BlizzardClient
from storage import ApplicationStore
import icons

load_dotenv()

# config.LOG_LEVEL controls every "wow-apply-bot"/"wow-apply-bot.<cog>"
# logger (all of this bot's own console output); config.LOG_DISCORD_LIBRARY_LEVEL
# controls discord.py's own internal logger separately, since DEBUG there
# is extremely noisy (gateway heartbeats etc.) and rarely what LOG_LEVEL=DEBUG
# is actually being set to chase down - see both constants' comments in
# config/deployment.py. An invalid level name falls back to its default
# rather than crashing on startup.
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _resolve_log_level(configured: str, fallback: str) -> str:
    name = (configured or "").upper()
    return name if name in _VALID_LOG_LEVELS else fallback


_bot_log_level = _resolve_log_level(config.LOG_LEVEL, "INFO")
logging.basicConfig(level=_bot_log_level)
log = logging.getLogger("wow-apply-bot")
if _bot_log_level != (config.LOG_LEVEL or "").upper():
    log.warning("config.LOG_LEVEL=%r isn't a valid logging level - using INFO instead", config.LOG_LEVEL)

logging.getLogger("discord").setLevel(_resolve_log_level(config.LOG_DISCORD_LIBRARY_LEVEL, "WARNING"))

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# Feature cogs to load. cogs.apply (guild applications) is the bot's core
# feature and always loads; every other cog is gated by a
# config.FEATURE_*_ENABLED flag, so a deployment that doesn't want e.g.
# raid summaries can turn the whole feature (and its commands) off without
# touching any code - see config/deployment.py's comment above those flags.
OPTIONAL_EXTENSIONS = [
    ("cogs.announcements", config.FEATURE_ANNOUNCEMENTS_ENABLED),
    ("cogs.emoji_admin", config.FEATURE_EMOJI_ADMIN_ENABLED),
    ("cogs.attendance", config.FEATURE_ATTENDANCE_ENABLED),
    ("cogs.raid_summary", config.FEATURE_RAID_SUMMARY_ENABLED),
    ("cogs.raid_logs", config.FEATURE_RAID_LOGS_ENABLED),
    ("cogs.tier_retrospective", config.FEATURE_TIER_RETROSPECTIVE_ENABLED),
]
EXTENSIONS = ["cogs.apply"] + [name for name, enabled in OPTIONAL_EXTENSIONS if enabled]

# raid_logs.py's automation needs raid_summary.py's and attendance.py's own
# cogs loaded to do its actual job, and tier_retrospective.py needs
# raid_summary.py's cached data (see config/deployment.py's comment above the
# FEATURE_* flags) - neither crashes without it (their bot.get_cog()
# lookups already degrade gracefully), but silently doing nothing isn't
# obvious from the console, so flag the misconfiguration loudly instead.
if config.FEATURE_RAID_LOGS_ENABLED and not config.FEATURE_RAID_SUMMARY_ENABLED:
    log.warning(
        "FEATURE_RAID_LOGS_ENABLED is True but FEATURE_RAID_SUMMARY_ENABLED is False - "
        "raid_logs.py's Summarize automation needs raid_summary.py's cog and won't work."
    )
if config.FEATURE_RAID_LOGS_ENABLED and not config.FEATURE_ATTENDANCE_ENABLED:
    log.warning(
        "FEATURE_RAID_LOGS_ENABLED is True but FEATURE_ATTENDANCE_ENABLED is False - "
        "raid_logs.py's main-raid automation needs attendance.py's cog and won't work."
    )
if config.FEATURE_TIER_RETROSPECTIVE_ENABLED and not config.FEATURE_RAID_SUMMARY_ENABLED:
    log.warning(
        "FEATURE_TIER_RETROSPECTIVE_ENABLED is True but FEATURE_RAID_SUMMARY_ENABLED is False - "
        "/tier-recap reads raid_summary.py's cached data and won't work."
    )

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
        disabled = [name for name, enabled in OPTIONAL_EXTENSIONS if not enabled]
        log.info("Loading extensions: %s", EXTENSIONS)
        if disabled:
            log.info("Disabled by config/deployment.py FEATURE_*_ENABLED flags: %s", disabled)
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