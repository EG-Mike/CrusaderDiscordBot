"""
Audit log, Discord side: records WHO did WHAT with the bot - every slash
command, context menu, button click, dropdown pick and modal submit - as a
structured event in audit.py's store (the dashboard's Audit log page reads
it), mirrored as one line in the plain-text file config.AUDIT_LOG_PATH
(rotated by size). Mods can pull recent entries into Discord with
/auditlog.

How it captures everything without touching any other cog: discord.py
dispatches an "interaction" event for EVERY incoming interaction before
routing it to the command/view that handles it, so a single on_interaction
listener here sees them all - including buttons on persistent views
registered by other cogs, and interactions that end up failing. New
features get audited automatically with no extra code.

What's recorded per action:
  - who: user ID, display name and username
  - where: channel ID and name, or "DM"
  - what:
      * slash command: full path incl. subcommand, plus option values
        (long text values truncated - see MAX_VALUE_LEN)
      * context menu: command name + target
      * button/select: custom_id, the button's label when it can be found
        on the message, selected values for dropdowns, and the message
      * modal submit: custom_id + which fields were filled in. Field
        CONTENTS are deliberately NOT recorded (applicant notes,
        announcement drafts etc.) - only their field IDs and lengths.
Autocomplete requests (fired on every keystroke while typing an option)
are skipped as noise.

On first start with this version, an existing audit.log (and its rotated
backups) is imported into the database once, so older history shows up on
the dashboard too.
"""

import asyncio
import io
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands

import audit
import config

log = logging.getLogger("wow-apply-bot.audit_log")

MAX_VALUE_LEN = 100       # truncate long option values (free-text fields)
MAX_LINES_PER_CALL = 500  # hard cap for /auditlog's `lines` option
INLINE_CHAR_LIMIT = 1900  # above this, /auditlog attaches a file instead

# Discord application command option types (raw API values).
_OPT_SUB_COMMAND = 1
_OPT_SUB_COMMAND_GROUP = 2
_OPT_USER = 6
_OPT_CHANNEL = 7
_OPT_ROLE = 8
_OPT_MENTIONABLE = 9
_OPT_ATTACHMENT = 11


def _clean(value) -> str:
    """One-line, tab-free, truncated string - keeps one action = one line."""
    text = str(value).replace("\t", " ").replace("\r", " ").replace("\n", "⏎")
    if len(text) > MAX_VALUE_LEN:
        text = text[: MAX_VALUE_LEN - 1] + "…"
    return text


def _resolved_name(data: dict, opt_type: int, value) -> str:
    """Turns a raw user/role/channel/attachment ID option into something
    readable, using the payload's own `resolved` block (no API calls)."""
    resolved = data.get("resolved") or {}
    key = str(value)
    if opt_type in (_OPT_USER, _OPT_MENTIONABLE) and key in resolved.get("users", {}):
        return f"@{resolved['users'][key].get('username', key)}({key})"
    if opt_type in (_OPT_ROLE, _OPT_MENTIONABLE) and key in resolved.get("roles", {}):
        return f"@{resolved['roles'][key].get('name', key)}({key})"
    if opt_type == _OPT_CHANNEL and key in resolved.get("channels", {}):
        return f"#{resolved['channels'][key].get('name', key)}({key})"
    if opt_type == _OPT_ATTACHMENT and key in resolved.get("attachments", {}):
        return f"file:{resolved['attachments'][key].get('filename', key)}"
    return _clean(value)


def describe_app_command(data: dict) -> dict:
    """Slash command / context menu payload -> audit event fields."""
    cmd_type = data.get("type", 1)
    name = _clean(data.get("name", "?"))

    # Context menus (2 = user, 3 = message): name + what it was used on.
    if cmd_type in (2, 3):
        kind = "user" if cmd_type == 2 else "message"
        target = data.get("target_id")
        return {
            "kind": "context_menu",
            "action": name,
            "summary": f'Used "{name}" on a {kind}',
            "target": target,
            "details": {"target_type": kind},
            "text": f'CONTEXT_MENU "{name}" on {kind} {target}',
        }

    parts = [name]
    options = data.get("options") or []
    # Walk down into subcommand groups / subcommands for the full path.
    while options and options[0].get("type") in (_OPT_SUB_COMMAND, _OPT_SUB_COMMAND_GROUP):
        parts.append(options[0].get("name", "?"))
        options = options[0].get("options") or []

    rendered = {
        str(opt.get("name")): _resolved_name(data, opt.get("type"), opt.get("value"))
        for opt in options
    }
    path = "/" + " ".join(parts)
    summary = path + "".join(f" {k}={v}" for k, v in rendered.items())
    return {
        "kind": "command",
        "action": path,
        "summary": summary,
        "details": {"options": rendered} if rendered else {},
        "text": "COMMAND " + summary,
    }


def _iter_components(components):
    """Yields every component on a message, recursing into action rows and
    components-v2 containers/sections (announcements use those)."""
    for component in components or []:
        yield component
        for attr in ("children", "components"):
            yield from _iter_components(getattr(component, attr, None))
        accessory = getattr(component, "accessory", None)
        if accessory is not None:
            yield accessory


def _find_component_label(message, custom_id: str):
    if message is None or not custom_id:
        return None
    try:
        for component in _iter_components(message.components):
            if getattr(component, "custom_id", None) == custom_id:
                label = getattr(component, "label", None) or getattr(component, "placeholder", None)
                emoji = getattr(component, "emoji", None)
                if emoji and label:
                    return f"{emoji} {label}"
                return label or (str(emoji) if emoji else None)
    except Exception:  # never let label lookup break auditing
        pass
    return None


def describe_component(data: dict, message=None) -> dict:
    """Button / dropdown payload -> audit event fields."""
    custom_id = _clean(data.get("custom_id", "?"))
    kind = "button" if data.get("component_type") == 2 else "select"
    label = _find_component_label(message, data.get("custom_id"))
    label = _clean(label) if label else None
    values = [_clean(v) for v in data.get("values") or []]

    details = {"custom_id": custom_id}
    if label:
        details["label"] = label
    if values:
        details["values"] = values
    message_id = str(message.id) if message is not None else None
    if message_id:
        details["message_id"] = message_id

    text = f"{kind.upper()} {custom_id}"
    if label:
        text += f' "{label}"'
    if values:
        text += " values=" + _clean(",".join(values))
    if message_id:
        text += f" msg={message_id}"
    return {
        "kind": kind,
        "action": label or custom_id,
        "summary": audit.component_summary(kind, label or custom_id, values),
        "target": message_id,
        "details": details,
        "text": text,
    }


def describe_modal(data: dict) -> dict:
    """Modal submit -> audit event fields. Only field IDs and sizes, never
    what was typed."""
    fields = []
    for row in data.get("components") or []:
        # Modal rows are either action rows wrapping inputs, or (newer
        # payloads) label components with a single `component`.
        inner = row.get("components") or ([row["component"]] if row.get("component") else [])
        for comp in inner:
            if "value" in comp:
                fields.append(f"{comp.get('custom_id')}({len(comp.get('value') or '')} chars)")
            elif "values" in comp:
                fields.append(f"{comp.get('custom_id')}({len(comp.get('values') or [])} picked)")
    custom_id = _clean(data.get("custom_id", "?"))
    text = f"MODAL {custom_id}"
    if fields:
        text += " fields=" + ",".join(fields)
    return {
        "kind": "modal",
        "action": custom_id,
        "summary": f"Submitted form {custom_id}",
        "details": {"custom_id": custom_id, "fields": fields} if fields else {"custom_id": custom_id},
        "text": text,
    }


def describe_interaction(interaction: discord.Interaction):
    """Any interaction -> (event fields, text-line "what"), or None for
    interactions that aren't user actions (autocomplete, ping)."""
    itype = interaction.type
    data = interaction.data or {}
    if itype == discord.InteractionType.application_command:
        return describe_app_command(data)
    if itype == discord.InteractionType.component:
        return describe_component(data, interaction.message)
    if itype == discord.InteractionType.modal_submit:
        return describe_modal(data)
    return None


def _where(interaction: discord.Interaction):
    """(channel_id, channel_name, text-line "where")."""
    if interaction.guild is None:
        return None, "DM", "DM"
    name = getattr(interaction.channel, "name", None)
    text = f"#{name}({interaction.channel_id})" if name else f"channel({interaction.channel_id})"
    return interaction.channel_id, name, text


def _who_text(user: discord.abc.User) -> str:
    display = getattr(user, "display_name", None) or user.name
    if display != user.name:
        return f"{display} [{user.name}] ({user.id})"
    return f"{user.name} ({user.id})"


def format_entry(event: dict) -> str:
    """One compact /auditlog line: time, who, where, what."""
    when = datetime.strptime(event["created_at"], audit.TIME_FORMAT).strftime("%Y-%m-%d %H:%M")
    who = event.get("actor_name") or event.get("actor_id") or "?"
    if event["source"] == "dashboard":
        where = "dashboard"
    elif event.get("channel_name") and event.get("channel_id"):
        where = f"#{event['channel_name']}"
    else:
        where = event.get("channel_name") or "?"
    return f"{when}  {who}  {where}  {event['summary']}"


class AuditLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.path = config.AUDIT_LOG_PATH
        self._handler = None

    async def cog_load(self):
        # Avoid stacking duplicate handlers if the cog is ever reloaded.
        for existing in list(audit.text_logger.handlers):
            audit.text_logger.removeHandler(existing)
            existing.close()
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        # Import the existing text file before attaching the handler, so
        # nothing written from now on is imported twice.
        try:
            imported = await asyncio.to_thread(
                audit.import_text_files, self.path, config.AUDIT_LOG_BACKUP_COUNT,
            )
            if imported:
                log.info("Audit log: imported %d earlier event(s) from %s into the database", imported, self.path)
        except Exception:
            log.exception("Audit log: importing %s into the database failed - carrying on without it", self.path)

        self._handler = RotatingFileHandler(
            self.path,
            maxBytes=config.AUDIT_LOG_MAX_BYTES,
            backupCount=config.AUDIT_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        audit.text_logger.addHandler(self._handler)
        log.info("Audit log: recording to the database and %s", os.path.abspath(self.path))

    async def cog_unload(self):
        if self._handler is not None:
            audit.text_logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    # --- permission check ------------------------------------------------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return False
        if member.guild_permissions.manage_roles:
            return True
        return bool(config.MOD_ROLE_ID) and any(role.id == config.MOD_ROLE_ID for role in member.roles)

    # --- capture -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            described = describe_interaction(interaction)
            if described is None:
                return  # autocomplete / ping: not user actions worth auditing
            user = interaction.user
            display = getattr(user, "display_name", None) or user.name
            channel_id, channel_name, where_text = _where(interaction)
            details = dict(described.get("details") or {})
            if display != user.name:
                details["username"] = user.name
            timestamp = audit.now()
            audit.record(
                source="discord",
                kind=described["kind"],
                action=described["action"],
                summary=described["summary"],
                actor_id=user.id,
                actor_name=display,
                channel_id=channel_id,
                channel_name=channel_name,
                target=described.get("target"),
                details=details,
                created_at=timestamp,
                text_line="\t".join(
                    (timestamp.replace("T", " "), _who_text(user), where_text, described["text"])
                ),
            )
        except Exception:
            # Auditing must never interfere with the actual interaction.
            log.exception("Failed to record audit event")

    # --- reading -----------------------------------------------------------

    @app_commands.command(name="auditlog", description="Show the most recent bot actions (who used which command/button)")
    @app_commands.describe(
        lines=f"How many recent entries to show (1-{MAX_LINES_PER_CALL}, default 25)",
        user="Only show actions by this member",
    )
    async def auditlog(
        self,
        interaction: discord.Interaction,
        lines: app_commands.Range[int, 1, MAX_LINES_PER_CALL] = 25,
        user: discord.User = None,
    ):
        if interaction.guild is None or not await self._is_mod(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Only moderators can view the audit log.", ephemeral=True)
            return

        # Fetch one extra: the newest entry is normally this very /auditlog
        # call (recorded a moment ago by on_interaction) - drop it as noise.
        filters = {"actor_id": user.id} if user else {}
        events, _ = await asyncio.to_thread(audit.search, filters, limit=lines + 1)
        if (
            events
            and events[0].get("actor_id") == str(interaction.user.id)
            and events[0]["kind"] == "command"
            and events[0]["action"] == "/auditlog"
        ):
            events = events[1:]
        events = list(reversed(events[:lines]))  # oldest first, like a log

        scope = f" by {user.mention}" if user else ""
        if not events:
            await interaction.response.send_message(f"No audit log entries{scope} yet.", ephemeral=True)
            return

        body = "\n".join(format_entry(e) for e in events)
        header = f"Last {len(events)} audit log entr{'y' if len(events) == 1 else 'ies'}{scope} (UTC):"
        if config.FEATURE_WEB_DASHBOARD_ENABLED:
            header += "\n-# Search and filter the full history on the dashboard's Audit log page."
        if len(body) + len(header) + 10 <= INLINE_CHAR_LIMIT:
            await interaction.response.send_message(
                f"{header}\n```\n{body}\n```",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            file = discord.File(io.BytesIO(body.encode("utf-8")), filename="audit_log.txt")
            await interaction.response.send_message(
                f"{header}\n(too long to show inline - attached)",
                file=file,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLogCog(bot))
