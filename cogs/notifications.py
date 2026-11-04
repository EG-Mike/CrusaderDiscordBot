"""
Notifications: the bot tells moderators about itself in a Discord channel.

Destination: NOTIFICATIONS_CHANNEL_ID, or the moderator chat channel
(MODERATOR_CHANNEL_ID) when that's empty. Each kind can be switched off; all
of these settings apply immediately (webapi/settings.py LIVE_PREFIXES).

What gets posted:
  - Bot status (NOTIFY_BOT_STATUS): one "back online" message after every
    start - with the new version and its title after an update, the reason
    and length of the stop, or a warning when the previous run ended
    unexpectedly - and a note when the Discord connection was down for a
    while. Built from the System log (system_log.py), since the bot can't
    post while it's stopping.
  - Errors (NOTIFY_ERRORS): a digest at most every NOTIFY_ERRORS_EVERY_MINUTES
    with how many errors happened and where. Never the error text: console
    details are admin-only on the dashboard, and this channel is for all
    moderators.
  - Dashboard changes (NOTIFY_DASHBOARD_CHANGES) and announcements
    (NOTIFY_ANNOUNCEMENTS): from the audit trail (audit.py), batched so a
    burst of changes (e.g. the setup guide) becomes one message.

Posting a notification never raises, and problems are logged as warnings,
not errors, so a broken channel can't feed the error reports.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord
from discord.ext import commands

import audit
import config
import db
import system_log

log = logging.getLogger("wow-apply-bot.notifications")

FLUSH_EVERY_SECONDS = 20
CHANGES_QUIET_SECONDS = 30      # post dashboard changes once they've stopped for this long...
CHANGES_MAX_WAIT_SECONDS = 120  # ...or when the oldest has waited this long
MAX_LINES = 15

DASHBOARD_CHANGE_KINDS = {"setting", "integration", "setup", "bot", "bot_message", "emoji"}
ANNOUNCEMENT_ACTIONS = {
    "announcements.publish": "published",
    "announcements.edit_published": "edited",
    "announcements.delete": "deleted",
}

COLORS = {"ok": 0x4AC880, "update": 0xDEAC52, "problem": 0xD64848, "warn": 0xECA03C, "info": 0x5865F2}


class NotificationError(Exception):
    """Why a notification can't be posted; the message says what to fix."""


def _timestamp(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int(datetime.strptime(iso, system_log.TIME_FORMAT).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h {rest // 60}m"


def source_label(logger_name: str) -> str:
    """'wow-apply-bot.raid_summary' -> 'Raid summary' (same wording as the
    dashboard's System log)."""
    if logger_name == "wow-apply-bot":
        return "Bot core"
    if logger_name.startswith("wow-apply-bot."):
        part = logger_name.split(".", 1)[1].replace("_", " ")
        return part[:1].upper() + part[1:]
    if logger_name.startswith("discord"):
        return "Discord library"
    if logger_name.startswith("aiohttp"):
        return "Web server"
    if logger_name.startswith("asyncio"):
        return "Python"
    return logger_name


def dashboard_link(path: str) -> Optional[str]:
    if not config.FEATURE_WEB_DASHBOARD_ENABLED:
        return None
    base = os.environ.get("WEB_BASE_URL", "http://localhost:8081").rstrip("/")
    return f"{base}{path}"


def back_online_embed(entries: List[dict]) -> Optional[discord.Embed]:
    """The "back online" message from the System log: `entries` are the
    lifecycle entries since the previous start, oldest first, ending with
    this start. None when there's no start to report."""
    if not entries or entries[-1]["event"] != "bot.started":
        return None
    started = entries[-1]
    by_event = {e["event"]: e for e in entries[:-1]}
    crashed, updated, stopped = by_event.get("bot.crashed"), by_event.get("bot.updated"), by_event.get("bot.stopped")
    version = started["details"].get("version") or "?"
    title_line = started["details"].get("title")

    if crashed:
        embed = discord.Embed(title="🔴 Back online after an unexpected stop", color=COLORS["problem"])
        lines = ["The bot stopped without shutting down cleanly (a crash, running out of memory, "
                 "or the server stopping it hard)."]
        down_since = _timestamp(crashed["details"].get("last_heartbeat"))
        if down_since:
            lines.append(f"Last sign of life: <t:{down_since}:f>.")
    elif updated:
        embed = discord.Embed(title="🔄 Updated and back online", color=COLORS["update"])
        lines = [f"Now running **{version}**" + (f": {title_line}" if title_line else "") + "."]
        if updated["details"].get("previous_version"):
            changes = updated["details"].get("compare_url")
            previous = updated["details"]["previous_version"]
            lines.append(f"Previous version: {previous}" + (f" ([what changed]({changes}))" if changes else "") + ".")
        down_since = _timestamp(stopped["created_at"]) if stopped else None
    else:
        embed = discord.Embed(title="🟢 Back online" if stopped else "🟢 Online", color=COLORS["ok"])
        lines = [f"Running version **{version}**" + (f": {title_line}" if title_line else "") + "."]
        down_since = _timestamp(stopped["created_at"]) if stopped else None

    if stopped and not crashed:
        reason = stopped["details"].get("reason") or stopped["message"]
        lines.append(f"Stopped because: {reason.rstrip('.')}.")
    back_at = _timestamp(started["created_at"])
    if down_since and back_at:
        lines.append(f"Offline for about {_duration(back_at - down_since)}.")
    embed.description = "\n".join(lines)
    return embed


class NotificationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.errors: Dict[str, int] = {}
        self.last_error_report = float("-inf")  # the first report goes out at the next flush
        self.changes: List[dict] = []
        self.ready_once = False
        self._unsubscribe = []
        self._task: Optional[asyncio.Task] = None
        self._warned: Optional[str] = None

    async def cog_load(self):
        loop = asyncio.get_running_loop()
        # Listeners may run on other threads: hand entries to the event loop.
        self._unsubscribe = [
            system_log.subscribe(lambda entry: loop.call_soon_threadsafe(self._on_system_entry, entry)),
            audit.subscribe(lambda event: loop.call_soon_threadsafe(self._on_audit_event, event)),
        ]
        self._task = loop.create_task(self._flush_loop())

    async def cog_unload(self):
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        if self._task is not None:
            self._task.cancel()

    # --- where notifications go -----------------------------------------------------------

    def destination(self) -> dict:
        """{channel_id, channel_name, source, problem} - also shown on the dashboard."""
        own = config.NOTIFICATIONS_CHANNEL_ID or 0
        channel_id = own or config.MODERATOR_CHANNEL_ID or 0
        source = "notifications" if own else ("moderator" if channel_id else None)
        info = {"channel_id": str(channel_id) if channel_id else None, "channel_name": None, "source": source,
                "problem": None}
        if not channel_id:
            info["problem"] = "No channel is set: choose a notifications channel (or a moderator chat channel)."
            return info
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            info["problem"] = "The bot can't see that channel. Pick another one, or give the bot access."
            return info
        info["channel_name"] = channel.name
        me = getattr(channel.guild, "me", None)
        perms = channel.permissions_for(me) if me is not None else None
        if perms is not None and not (perms.view_channel and perms.send_messages and perms.embed_links):
            info["problem"] = f"The bot needs View Channel, Send Messages and Embed Links in #{channel.name}."
        return info

    async def post(self, embed: discord.Embed) -> str:
        """Posts a notification; returns the channel name. Raises
        NotificationError with what to fix."""
        info = self.destination()
        if info["problem"]:
            raise NotificationError(info["problem"])
        embed.set_footer(text="Notifications · change them in the dashboard: Settings, Notifications")
        channel = self.bot.get_channel(int(info["channel_id"]))
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            raise NotificationError(f"Discord refused the message: {exc.text or exc.status}") from None
        return channel.name

    async def _post_quietly(self, embed: discord.Embed):
        try:
            await self.post(embed)
            self._warned = None
        except NotificationError as exc:
            if self._warned != str(exc):  # say it once, not every flush
                log.warning("Couldn't post a notification: %s", exc)
                self._warned = str(exc)

    async def send_test(self, by: str) -> str:
        embed = discord.Embed(
            title="✅ Test notification",
            description=f"Notifications from the bot will appear here. Sent from the dashboard by {by}.",
            color=COLORS["info"],
        )
        return await self.post(embed)

    # --- collecting -------------------------------------------------------------------------

    def _on_system_entry(self, entry: dict):
        if entry["category"] == "console":
            if entry["level"] in ("ERROR", "CRITICAL") and not entry["event"].startswith(log.name):
                label = source_label(entry["details"].get("logger") or entry["event"])
                self.errors[label] = self.errors.get(label, 0) + 1
        elif entry["event"] == "discord.outage" and config.NOTIFY_BOT_STATUS and self.ready_once:
            embed = discord.Embed(title="🟠 Discord connection restored", description=entry["message"],
                                  color=COLORS["warn"])
            asyncio.get_running_loop().create_task(self._post_quietly(embed))

    def _on_audit_event(self, event: dict):
        if event["source"] != "dashboard":
            return
        if event["kind"] in DASHBOARD_CHANGE_KINDS or event["action"] in ANNOUNCEMENT_ACTIONS:
            self.changes.append({**event, "_at": time.monotonic()})

    # --- posting ----------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        if self.ready_once:
            return
        self.ready_once = True
        if not config.NOTIFY_BOT_STATUS:
            return
        embed = await asyncio.to_thread(self._back_online)
        if embed is not None:
            await self._post_quietly(embed)

    def _back_online(self) -> Optional[discord.Embed]:
        starts = db.query("SELECT id FROM system_log WHERE event = 'bot.started' ORDER BY id DESC LIMIT 2")
        if not starts:
            return None
        previous = starts[1]["id"] if len(starts) > 1 else 0
        rows = db.query("SELECT * FROM system_log WHERE category = 'lifecycle' AND id > ? AND id <= ? ORDER BY id",
                        (previous, starts[0]["id"]))
        return back_online_embed([system_log.serialize(r) for r in rows])

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_EVERY_SECONDS)
            try:
                await self.flush()
            except Exception:
                log.warning("Notification flush failed", exc_info=True)

    async def flush(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        if not self.ready_once:
            return
        if self.errors and now - self.last_error_report >= max(1, int(config.NOTIFY_ERRORS_EVERY_MINUTES)) * 60:
            errors, self.errors = self.errors, {}
            self.last_error_report = now
            if config.NOTIFY_ERRORS:
                await self._post_quietly(self._errors_embed(errors))
        if self.changes and (now - self.changes[-1]["_at"] >= CHANGES_QUIET_SECONDS
                             or now - self.changes[0]["_at"] >= CHANGES_MAX_WAIT_SECONDS):
            changes, self.changes = self.changes, []
            for embed in self._change_embeds(changes):
                await self._post_quietly(embed)

    def _errors_embed(self, errors: Dict[str, int]) -> discord.Embed:
        total = sum(errors.values())
        lines = [f"• {label}: {count}" for label, count in sorted(errors.items(), key=lambda kv: -kv[1])][:MAX_LINES]
        link = dashboard_link("/system-log?view=errors&range=24h")
        if link:
            lines.append(f"\nDetails are in the dashboard's [System log]({link}) (admins).")
        return discord.Embed(title=f"⚠️ {total} error{'s' if total != 1 else ''} in the bot's console",
                             description="\n".join(lines), color=COLORS["problem"])

    def _change_embeds(self, changes: List[dict]) -> List[discord.Embed]:
        embeds = []
        dashboard = [c for c in changes if c["kind"] in DASHBOARD_CHANGE_KINDS]
        announced = [c for c in changes if c["action"] in ANNOUNCEMENT_ACTIONS]
        if dashboard and config.NOTIFY_DASHBOARD_CHANGES:
            lines = [f"• **{c['actor_name'] or 'Someone'}**: {c['summary']}" for c in dashboard[:MAX_LINES]]
            if len(dashboard) > MAX_LINES:
                lines.append(f"…and {len(dashboard) - MAX_LINES} more.")
            link = dashboard_link("/audit-log?source=dashboard")
            if link:
                lines.append(f"\nFull history: [Audit log]({link}).")
            embeds.append(discord.Embed(title="🛠️ Changes on the dashboard", description="\n".join(lines),
                                        color=COLORS["info"]))
        if announced and config.NOTIFY_ANNOUNCEMENTS:
            guild_id = getattr(next(iter(self.bot.guilds), None), "id", None)
            lines = []
            for c in announced[:MAX_LINES]:
                line = f"• **{c['actor_name'] or 'Someone'}**: {c['summary']}"
                channel_id, message_id = c["details"].get("channel_id"), c["details"].get("message_id")
                if guild_id and channel_id and message_id and c["action"] != "announcements.delete":
                    line += f" ([open](https://discord.com/channels/{guild_id}/{channel_id}/{message_id}))"
                lines.append(line)
            embeds.append(discord.Embed(title="📣 Announcements", description="\n".join(lines), color=COLORS["update"]))
        return embeds


async def setup(bot: commands.Bot):
    await bot.add_cog(NotificationsCog(bot))
