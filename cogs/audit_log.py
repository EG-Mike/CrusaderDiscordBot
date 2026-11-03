"""
Audit log: records WHO did WHAT with the bot - every slash command, context
menu, button click, dropdown pick and modal submit - one line per action,
to a plain-text file (config.AUDIT_LOG_PATH, rotated by size so it never
grows unbounded). Mods can pull the most recent lines into Discord with
/auditlog.

How it captures everything without touching any other cog: discord.py
dispatches an "interaction" event for EVERY incoming interaction before
routing it to the command/view that handles it, so a single on_interaction
listener here sees them all - including buttons on persistent views
registered by other cogs, and interactions that end up failing. New
features get audited automatically with no extra code.

What's logged per line (tab-separated, UTC timestamp first):
  - who: display name + username + user ID (the ID is what /auditlog's
    user filter matches on, since names change)
  - where: guild channel name + ID, or "DM"
  - what:
      * slash command: full path incl. subcommand, plus option values
        (long text values truncated - see MAX_VALUE_LEN)
      * context menu: command name + target
      * button/select: custom_id, the button's label when it can be found
        on the message, and selected values for dropdowns
      * modal submit: custom_id + which fields were filled in. Field
        CONTENTS are deliberately NOT logged (applicant notes,
        announcement drafts etc. would otherwise land in a mod-readable
        file) - only their field IDs and lengths.
Autocomplete requests (fired on every keystroke while typing an option)
are skipped as noise.
"""

import os
import logging
import io
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("wow-apply-bot.audit_log")

# Dedicated logger that ONLY writes to the audit file (propagate=False), so
# audit lines don't also spam the console.
audit_logger = logging.getLogger("wow-apply-bot.audit")
audit_logger.propagate = False
audit_logger.setLevel(logging.INFO)

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


def _describe_app_command(data: dict) -> str:
    cmd_type = data.get("type", 1)
    name = data.get("name", "?")

    # Context menus (2 = user, 3 = message): name + what it was used on.
    if cmd_type in (2, 3):
        target = data.get("target_id")
        kind = "user" if cmd_type == 2 else "message"
        return f"CONTEXT_MENU \"{_clean(name)}\" on {kind} {target}"

    parts = [name]
    options = data.get("options") or []
    # Walk down into subcommand groups / subcommands for the full path.
    while options and options[0].get("type") in (_OPT_SUB_COMMAND, _OPT_SUB_COMMAND_GROUP):
        parts.append(options[0].get("name", "?"))
        options = options[0].get("options") or []

    rendered = [
        f"{opt.get('name')}={_resolved_name(data, opt.get('type'), opt.get('value'))}"
        for opt in options
    ]
    return "COMMAND /" + " ".join(parts) + (" " + " ".join(rendered) if rendered else "")


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


def _describe_component(interaction: discord.Interaction) -> str:
    data = interaction.data or {}
    custom_id = data.get("custom_id", "?")
    component_type = data.get("component_type")
    label = _find_component_label(interaction.message, custom_id)

    kind = "BUTTON" if component_type == 2 else "SELECT"
    text = f"{kind} {_clean(custom_id)}"
    if label:
        text += f" \"{_clean(label)}\""
    values = data.get("values")
    if values:
        text += " values=" + _clean(",".join(str(v) for v in values))
    if interaction.message is not None:
        text += f" msg={interaction.message.id}"
    return text


def _describe_modal(interaction: discord.Interaction) -> str:
    data = interaction.data or {}
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
    text = f"MODAL {_clean(data.get('custom_id', '?'))}"
    if fields:
        text += " fields=" + ",".join(fields)
    return text


def _describe_where(interaction: discord.Interaction) -> str:
    channel = interaction.channel
    if interaction.guild is None:
        return "DM"
    name = getattr(channel, "name", None)
    return f"#{name}({interaction.channel_id})" if name else f"channel({interaction.channel_id})"


def _describe_who(user: discord.abc.User) -> str:
    display = getattr(user, "display_name", None) or user.name
    if display != user.name:
        return f"{display} [{user.name}] ({user.id})"
    return f"{user.name} ({user.id})"


def _who_field(line: str) -> str:
    fields = line.split("\t")
    return fields[1] if len(fields) > 1 else ""


class AuditLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.path = config.AUDIT_LOG_PATH
        self._handler = None

    async def cog_load(self):
        # Avoid stacking duplicate handlers if the cog is ever reloaded.
        for existing in list(audit_logger.handlers):
            audit_logger.removeHandler(existing)
            existing.close()
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._handler = RotatingFileHandler(
            self.path,
            maxBytes=config.AUDIT_LOG_MAX_BYTES,
            backupCount=config.AUDIT_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(self._handler)
        log.info("Audit log writing to %s", os.path.abspath(self.path))

    async def cog_unload(self):
        if self._handler is not None:
            audit_logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    # --- permission check ------------------------------------------------

    async def _is_mod(self, guild: discord.Guild, user_id: int) -> bool:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member is None:
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    # --- capture -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            itype = interaction.type
            if itype == discord.InteractionType.application_command:
                what = _describe_app_command(interaction.data or {})
            elif itype == discord.InteractionType.component:
                what = _describe_component(interaction)
            elif itype == discord.InteractionType.modal_submit:
                what = _describe_modal(interaction)
            else:
                return  # autocomplete / ping: not user actions worth auditing

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            audit_logger.info(
                "%s\t%s\t%s\t%s",
                timestamp, _describe_who(interaction.user), _describe_where(interaction), what,
            )
        except Exception:
            # Auditing must never interfere with the actual interaction.
            log.exception("Failed to write audit log entry")

    # --- reading -----------------------------------------------------------

    def _tail(self, count: int, user_id=None) -> list:
        """Last `count` lines (optionally only one user's), reading the
        rotated backups too when the current file alone doesn't have enough."""
        files = [self.path] + [f"{self.path}.{i}" for i in range(1, config.AUDIT_LOG_BACKUP_COUNT + 1)]
        needle = f"({user_id})" if user_id else None
        collected = []  # newest file first; each chunk is in chronological order
        remaining = count
        for path in files:
            if remaining <= 0 or not os.path.exists(path):
                break  # done, or no older backups exist
            chunk = deque(maxlen=remaining)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Match only the "who" column (2nd field), not channel IDs.
                    if needle is None or _who_field(line).endswith(needle):
                        chunk.append(line.rstrip("\n"))
            collected.insert(0, list(chunk))
            remaining -= len(chunk)
        return [line for chunk in collected for line in chunk]

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

        # Fetch one extra: the newest line is normally this very /auditlog
        # call (logged a moment ago by on_interaction) - drop it as noise.
        entries = self._tail(lines + 1, user.id if user else None)
        if (
            entries
            and _who_field(entries[-1]).endswith(f"({interaction.user.id})")
            and "\tCOMMAND /auditlog" in entries[-1]
        ):
            entries = entries[:-1]
        entries = entries[-lines:]

        scope = f" by {user.mention}" if user else ""
        if not entries:
            await interaction.response.send_message(f"No audit log entries{scope} yet.", ephemeral=True)
            return

        body = "\n".join(entries)
        header = f"Last {len(entries)} audit log entr{'y' if len(entries) == 1 else 'ies'}{scope} (UTC):"
        if len(body) + len(header) + 10 <= INLINE_CHAR_LIMIT:
            await interaction.response.send_message(
                f"{header}\n```\n{body}\n```",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            file = discord.File(io.BytesIO(body.encode("utf-8")), filename="audit_log.txt")
            await interaction.response.send_message(
                f"{header} (too long to show inline - attached)",
                file=file,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLogCog(bot))
