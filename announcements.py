"""
Announcements: drafts, publishing, editing, version history and templates.

One data model shared by the two ways of working with announcements:
  - Discord: /announce posts a draft in the sandbox channel with Edit and
    Publish buttons (cogs/announcements.py).
  - The dashboard's announcement builder (webapi/announcements_api.py):
    drafts that don't need a Discord message until they're published.

An announcement is one row in the `announcements` table (db.py), linked to
Discord by up to two message IDs: `sandbox_message_id` (its /announce
draft, if made in Discord) and `message_id` (the published message). The
cog finds announcements by either ID, so buttons on existing messages keep
working.

Every publish and every later edit of the published message stores a copy
in `announcement_versions` (the agreed exception to the "no content in the
audit log" rule lives here, not in the audit log). Templates are reusable
starting points (`announcement_templates`).

Messages are Discord "components v2" layouts: a container with an accent
colour, the title as a heading, and the body split into sections on lines
that are exactly `---`. Text follows Discord's own markdown.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord

import config
import db

log = logging.getLogger("wow-apply-bot.announcements")

DEFAULT_COLOR = "#5865F2"     # Discord blurple, what announcements always used
MAX_TITLE = 256
MAX_TOTAL_TEXT = 4000         # Discord's limit for all text in one message
MAX_TEMPLATE_NAME = 80
STATUSES = ("draft", "published")
EDITABLE_FIELDS = ("title", "body", "color", "ping")

# Persistent button IDs (registered by cogs/announcements.py).
DRAFT_EDIT_ID = "wow_announce_draft_edit"
DRAFT_PUBLISH_ID = "wow_announce_draft_publish"
EDIT_PUBLISHED_ID = "wow_announce_editpublished"

# custom_id -> coroutine, set by cogs/announcements.py. Every view built here
# uses them: editing a message with a view whose buttons have no callbacks
# would bind that dead view to the message and shadow the persistent one.
_BUTTON_HANDLERS: Dict[str, object] = {}

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_PING_RE = re.compile(r"^(everyone|here|role:\d{1,20})$")


class AnnouncementError(Exception):
    """Something the person can fix; the message says what."""


class NotFound(AnnouncementError):
    pass


class Conflict(AnnouncementError):
    """The announcement changed since the editor loaded it."""

    def __init__(self, current: dict):
        super().__init__("Someone else changed this announcement since you opened it.")
        self.current = current


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- validation --------------------------------------------------------------------

def clean_fields(fields: dict) -> dict:
    """Validates and normalises editable fields (only those present).
    Raises AnnouncementError with a message for the editor."""
    out = {}
    if "title" in fields:
        title = (fields["title"] or "").strip()
        if len(title) > MAX_TITLE:
            raise AnnouncementError(f"The title can be at most {MAX_TITLE} characters.")
        out["title"] = title or None
    if "body" in fields:
        body = fields["body"] or ""
        if not isinstance(body, str):
            raise AnnouncementError("The text must be text.")
        out["body"] = body.replace("\r\n", "\n").strip("\n")
    if "color" in fields:
        color = (fields["color"] or "").strip()
        if color and not _COLOR_RE.match(color):
            raise AnnouncementError("The colour must look like #5865F2.")
        out["color"] = color.upper() if color else None
    if "ping" in fields:
        ping = (fields["ping"] or "").strip()
        if ping and not _PING_RE.match(ping):
            raise AnnouncementError("Unknown ping option.")
        out["ping"] = ping or None
    return out


def check_publishable(record: dict):
    """Rules for anything that's about to be sent to Discord."""
    if not (record.get("body") or "").strip():
        raise AnnouncementError("Write some text first: the announcement is empty.")
    total = len(record.get("title") or "") + len(record.get("body") or "")
    if total > MAX_TOTAL_TEXT - 20:
        raise AnnouncementError(
            f"The title and text together are {total} characters; Discord allows about {MAX_TOTAL_TEXT - 20}."
        )


# --- rendering ----------------------------------------------------------------------

def split_sections(body: str) -> List[str]:
    """Body text -> sections, split on lines that are exactly '---'."""
    sections, current = [], []
    for line in (body or "").split("\n"):
        if line.strip() == "---":
            sections.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    sections.append("\n".join(current).strip())
    return [s for s in sections if s]


def _color(record: dict) -> discord.Color:
    return discord.Color(int((record.get("color") or DEFAULT_COLOR)[1:], 16))


def ping_text(ping: Optional[str]) -> Optional[str]:
    if not ping:
        return None
    if ping in ("everyone", "here"):
        return f"@{ping}"
    return f"<@&{ping.split(':', 1)[1]}>"


def allowed_mentions(ping: Optional[str]) -> discord.AllowedMentions:
    """Only the chosen ping may notify anyone; mentions typed in the text
    (users, roles) show as mentions but don't ping."""
    if ping in ("everyone", "here"):
        return discord.AllowedMentions(everyone=True, users=False, roles=False)
    if ping and ping.startswith("role:"):
        return discord.AllowedMentions(everyone=False, users=False,
                                       roles=[discord.Object(int(ping.split(":", 1)[1]))])
    return discord.AllowedMentions.none()


def build_container(record: dict) -> discord.ui.Container:
    container = discord.ui.Container(accent_color=_color(record))
    if record.get("title"):
        container.add_item(discord.ui.TextDisplay(f"## {record['title']}"))
        container.add_item(discord.ui.Separator())
    sections = split_sections(record.get("body") or "") or ["​"]
    for i, section in enumerate(sections):
        container.add_item(discord.ui.TextDisplay(section))
        if i < len(sections) - 1:
            container.add_item(discord.ui.Separator())
    return container


def published_view(record: dict) -> discord.ui.LayoutView:
    """The public message: optional ping line, then the announcement."""
    view = discord.ui.LayoutView(timeout=None)
    ping = ping_text(record.get("ping"))
    if ping:
        view.add_item(discord.ui.TextDisplay(ping))
    view.add_item(build_container(record))
    return view


def set_button_handlers(handlers: Dict[str, object]):
    """Called by the cog: the coroutines behind the sandbox buttons."""
    _BUTTON_HANDLERS.clear()
    _BUTTON_HANDLERS.update(handlers)


def _button(label: str, style: discord.ButtonStyle, custom_id: str):
    button = discord.ui.Button(label=label, style=style, custom_id=custom_id)
    if custom_id in _BUTTON_HANDLERS:
        button.callback = _BUTTON_HANDLERS[custom_id]
    return button


def draft_view(record: dict) -> discord.ui.LayoutView:
    """The sandbox draft with Edit/Publish buttons."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_container(record))
    view.add_item(discord.ui.TextDisplay("-# 📝 Draft - not yet published. Edit it here or on the dashboard."))
    row = discord.ui.ActionRow()
    row.add_item(_button("✏️ Edit", discord.ButtonStyle.secondary, DRAFT_EDIT_ID))
    row.add_item(_button("🚀 Publish", discord.ButtonStyle.success, DRAFT_PUBLISH_ID))
    view.add_item(row)
    return view


def published_marker_view(record: dict) -> discord.ui.LayoutView:
    """What the sandbox draft becomes once published: the same preview, a
    'Published to #channel' line, and an Edit button for the published
    message (moderator-only, since the sandbox is)."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_container(record))
    where = f"<#{record['channel_id']}>" if record.get("channel_id") else "a channel"
    view.add_item(discord.ui.TextDisplay(f"-# ✅ Published to {where} by {record.get('published_by') or 'a moderator'}"))
    row = discord.ui.ActionRow()
    row.add_item(_button("✏️ Edit", discord.ButtonStyle.secondary, EDIT_PUBLISHED_ID))
    view.add_item(row)
    return view


# --- reading ------------------------------------------------------------------------

def _row(row) -> dict:
    return dict(row) if row is not None else None


def get(announcement_id) -> dict:
    rows = db.query("SELECT * FROM announcements WHERE id = ?", (int(announcement_id),))
    if not rows:
        raise NotFound("This announcement doesn't exist (any more).")
    return _row(rows[0])


def find_by_message(message_id) -> Optional[dict]:
    """The announcement whose sandbox draft or published message this is."""
    rows = db.query(
        "SELECT * FROM announcements WHERE sandbox_message_id = ? OR message_id = ? ORDER BY id DESC LIMIT 1",
        (str(message_id), str(message_id)),
    )
    return _row(rows[0]) if rows else None


def list_announcements(status: Optional[str] = None, q: Optional[str] = None, limit: int = 200) -> List[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
        params += [like, like]
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(f"SELECT * FROM announcements{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (*params, max(1, min(limit, 500))))
    return [_row(r) for r in rows]


def counts() -> Dict[str, int]:
    rows = db.query("SELECT status, COUNT(*) AS n FROM announcements GROUP BY status")
    result = {status: 0 for status in STATUSES}
    result.update({r["status"]: r["n"] for r in rows})
    return result


def versions(announcement_id) -> List[dict]:
    rows = db.query(
        "SELECT id, event, title, body, color, saved_at, saved_by FROM announcement_versions "
        "WHERE announcement_id = ? ORDER BY id DESC", (int(announcement_id),),
    )
    return [_row(r) for r in rows]


# --- writing (database only) ----------------------------------------------------------

def create(fields: dict, by: str, *, sandbox_message_id=None) -> dict:
    data = {"title": None, "body": "", "color": None, "ping": None}
    data.update(clean_fields(fields))
    stamp = _now()
    cursor = db.execute(
        "INSERT INTO announcements (status, title, body, color, ping, sandbox_message_id, created_at, created_by, "
        "updated_at, updated_by) VALUES ('draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["title"], data["body"], data["color"], data["ping"],
         str(sandbox_message_id) if sandbox_message_id else None, stamp, by, stamp, by),
    )
    return get(cursor.lastrowid)


def update(announcement_id, fields: dict, by: str, *, revision: Optional[int] = None) -> dict:
    """Saves editable fields. With `revision`, refuses (Conflict) if the
    announcement changed since that revision was loaded."""
    current = get(announcement_id)
    if revision is not None and int(revision) != current["revision"]:
        raise Conflict(current)
    changes = clean_fields({k: v for k, v in fields.items() if k in EDITABLE_FIELDS})
    if current["status"] == "published" and "ping" in changes and changes["ping"] != current["ping"]:
        raise AnnouncementError("The ping can't be changed after publishing (Discord doesn't ping on edits).")
    if not changes:
        return current
    sets = ", ".join(f"{k} = ?" for k in changes)
    db.execute(
        f"UPDATE announcements SET {sets}, revision = revision + 1, updated_at = ?, updated_by = ? WHERE id = ?",
        (*changes.values(), _now(), by, current["id"]),
    )
    return get(current["id"])


def set_sandbox_message(announcement_id, message_id):
    db.execute("UPDATE announcements SET sandbox_message_id = ? WHERE id = ?",
               (str(message_id) if message_id else None, int(announcement_id)))


def delete(announcement_id):
    db.execute("DELETE FROM announcements WHERE id = ?", (int(announcement_id),))


def _add_version(record: dict, event: str, by: str):
    db.execute(
        "INSERT INTO announcement_versions (announcement_id, event, title, body, color, saved_at, saved_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (record["id"], event, record.get("title"), record.get("body") or "", record.get("color"), _now(), by),
    )


# --- Discord ----------------------------------------------------------------------

def _sandbox_channel(bot):
    channel_id = config.ANNOUNCEMENT_SANDBOX_CHANNEL_ID
    return bot.get_channel(int(channel_id)) if channel_id else None


async def refresh_sandbox(bot, record: dict):
    """Keeps an announcement's /announce sandbox draft (if any) showing its
    current content and state. Best effort: a deleted draft is fine."""
    if not record.get("sandbox_message_id"):
        return
    channel = _sandbox_channel(bot)
    if channel is None:
        return
    view = published_marker_view(record) if record["status"] == "published" else draft_view(record)
    try:
        message = await channel.fetch_message(int(record["sandbox_message_id"]))
        await message.edit(view=view)
    except (discord.NotFound, discord.Forbidden):
        pass
    except discord.HTTPException:
        log.warning("Couldn't update the sandbox draft of announcement %s", record["id"], exc_info=True)


def _publish_target(bot, channel_id):
    channel = bot.get_channel(int(channel_id)) if str(channel_id or "").isdigit() else None
    if channel is None or not isinstance(channel, (discord.TextChannel,)):
        raise AnnouncementError("Pick a text or announcement channel the bot can see.")
    return channel


def _check_permissions(channel, ping: Optional[str]):
    me = channel.guild.me
    perms = channel.permissions_for(me)
    if not (perms.view_channel and perms.send_messages):
        raise AnnouncementError(f"The bot can't post in #{channel.name}: give it View Channel and Send Messages there.")
    if ping in ("everyone", "here") and not perms.mention_everyone:
        raise AnnouncementError(
            f"The bot isn't allowed to ping @{ping} in #{channel.name}: give it the Mention Everyone permission, "
            "or choose another ping."
        )
    if ping and ping.startswith("role:"):
        role = channel.guild.get_role(int(ping.split(":", 1)[1]))
        if role is None:
            raise AnnouncementError("The role to ping doesn't exist any more.")
        if not role.mentionable and not perms.mention_everyone:
            raise AnnouncementError(
                f"The bot can't ping @{role.name}: make the role mentionable, or give the bot Mention Everyone."
            )


def _discord_error(exc: discord.HTTPException) -> AnnouncementError:
    return AnnouncementError(f"Discord refused the message: {exc.text or exc.status}")


async def publish(bot, announcement_id, channel_id, by: str, *, revision: Optional[int] = None) -> dict:
    """Posts a draft to a channel and marks it published."""
    record = get(announcement_id)
    if revision is not None and int(revision) != record["revision"]:
        raise Conflict(record)
    if record["status"] != "draft":
        raise AnnouncementError("This announcement has already been published.")
    check_publishable(record)
    channel = _publish_target(bot, channel_id)
    _check_permissions(channel, record.get("ping"))
    try:
        message = await channel.send(view=published_view(record), allowed_mentions=allowed_mentions(record.get("ping")))
    except discord.HTTPException as exc:
        raise _discord_error(exc) from None
    stamp = _now()
    db.execute(
        "UPDATE announcements SET status = 'published', channel_id = ?, message_id = ?, published_at = ?, "
        "published_by = ?, updated_at = ?, updated_by = ?, revision = revision + 1 WHERE id = ?",
        (str(channel.id), str(message.id), stamp, by, stamp, by, record["id"]),
    )
    record = get(record["id"])
    _add_version(record, "published", by)
    await refresh_sandbox(bot, record)
    return record


async def _published_message(bot, record: dict):
    channel = bot.get_channel(int(record["channel_id"])) if record.get("channel_id") else None
    if channel is None:
        raise AnnouncementError("The channel this was published in doesn't exist or the bot can't see it.")
    try:
        return await channel.fetch_message(int(record["message_id"]))
    except discord.NotFound:
        raise AnnouncementError("The published message was deleted in Discord.") from None
    except discord.Forbidden:
        raise AnnouncementError(f"The bot can't read #{channel.name} any more.") from None


async def edit_published(bot, announcement_id, fields: dict, by: str, *, revision: Optional[int] = None) -> dict:
    """Changes a published announcement: Discord first, then the database,
    so the two never disagree. Edits never ping anyone again."""
    record = get(announcement_id)
    if revision is not None and int(revision) != record["revision"]:
        raise Conflict(record)
    if record["status"] != "published":
        raise AnnouncementError("This announcement isn't published yet.")
    changes = clean_fields({k: v for k, v in fields.items() if k in ("title", "body", "color")})
    preview = {**record, **changes}
    check_publishable(preview)
    message = await _published_message(bot, record)
    try:
        await message.edit(view=published_view(preview), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        raise _discord_error(exc) from None
    record = update(record["id"], changes, by) if changes else record
    _add_version(record, "edited", by)
    await refresh_sandbox(bot, record)
    return record


async def delete_everywhere(bot, announcement_id) -> List[str]:
    """Deletes the announcement, its published message and its sandbox
    draft. Returns notes about anything in Discord that couldn't be removed."""
    record = get(announcement_id)
    notes = []
    if record["status"] == "published":
        try:
            message = await _published_message(bot, record)
            await message.delete()
        except AnnouncementError as exc:
            notes.append(str(exc))
        except discord.HTTPException as exc:
            notes.append(f"Couldn't delete the published message: {exc.text or exc.status}")
    if record.get("sandbox_message_id"):
        channel = _sandbox_channel(bot)
        if channel is not None:
            try:
                await (await channel.fetch_message(int(record["sandbox_message_id"]))).delete()
            except discord.HTTPException:
                pass
    delete(record["id"])
    return notes


async def remove_legacy_buttons(bot) -> Tuple[int, List[str]]:
    """Re-renders every published announcement without buttons (very early
    versions put a public Edit button on them). Safe to run repeatedly.
    Returns (updated, [message IDs that couldn't be reached])."""
    updated, unreachable = 0, []
    for record in list_announcements(status="published", limit=500):
        try:
            message = await _published_message(bot, record)
            await message.edit(view=published_view(record), allowed_mentions=discord.AllowedMentions.none())
            updated += 1
        except (AnnouncementError, discord.HTTPException):
            unreachable.append(str(record.get("message_id")))
    return updated, unreachable


# --- templates -------------------------------------------------------------------------

def list_templates() -> List[dict]:
    return [_row(r) for r in db.query("SELECT * FROM announcement_templates ORDER BY name COLLATE NOCASE")]


def get_template(template_id) -> dict:
    rows = db.query("SELECT * FROM announcement_templates WHERE id = ?", (int(template_id),))
    if not rows:
        raise NotFound("This template doesn't exist (any more).")
    return _row(rows[0])


def _template_name(name) -> str:
    name = (name or "").strip()
    if not name:
        raise AnnouncementError("Give the template a name.")
    if len(name) > MAX_TEMPLATE_NAME:
        raise AnnouncementError(f"Template names can be at most {MAX_TEMPLATE_NAME} characters.")
    return name


def create_template(name: str, fields: dict, by: str) -> dict:
    data = {"title": None, "body": "", "color": None, "ping": None}
    data.update(clean_fields(fields))
    stamp = _now()
    cursor = db.execute(
        "INSERT INTO announcement_templates (name, title, body, color, ping, created_at, created_by, updated_at, "
        "updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_template_name(name), data["title"], data["body"], data["color"], data["ping"], stamp, by, stamp, by),
    )
    return get_template(cursor.lastrowid)


def update_template(template_id, name: Optional[str], fields: dict, by: str) -> dict:
    template = get_template(template_id)
    changes = clean_fields({k: v for k, v in fields.items() if k in EDITABLE_FIELDS})
    if name is not None:
        changes["name"] = _template_name(name)
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        db.execute(f"UPDATE announcement_templates SET {sets}, updated_at = ?, updated_by = ? WHERE id = ?",
                   (*changes.values(), _now(), by, template["id"]))
    return get_template(template["id"])


def delete_template(template_id):
    db.execute("DELETE FROM announcement_templates WHERE id = ?", (int(template_id),))


# --- importing records from before the database ------------------------------------------

def import_legacy(records: List[tuple]) -> int:
    """One-off import of the old announcements store (message ID -> record,
    see storage.py) into the table. Drafts and the published message they
    led to become one announcement. Runs once per installation."""
    if db.get_meta("announcements_imported_at"):
        return 0
    by_key = {str(k): v for k, v in records if isinstance(v, dict)}
    linked_published = set()
    rows = []
    stamp = _now()
    for key, rec in by_key.items():
        if not rec.get("is_draft"):
            continue
        published_id = str(rec.get("published_message_id") or "")
        published = by_key.get(published_id, {}) if published_id else {}
        if published_id:
            linked_published.add(published_id)
        is_published = bool(rec.get("published"))
        rows.append((
            "published" if is_published else "draft",
            published.get("title", rec.get("title")), published.get("body", rec.get("body")) or "",
            str(rec.get("published_channel_id") or "") or None if is_published else None,
            published_id or None if is_published else None, key,
            rec.get("published_by") if is_published else None,
        ))
    for key, rec in by_key.items():
        if rec.get("is_draft") or key in linked_published or not rec.get("channel_id"):
            continue
        rows.append(("published", rec.get("title"), rec.get("body") or "", str(rec["channel_id"]), key, None,
                     str(rec.get("created_by") or "") or None))
    with db.transaction() as conn:
        for status, title, body, channel_id, message_id, sandbox_id, published_by in rows:
            conn.execute(
                "INSERT INTO announcements (status, title, body, channel_id, message_id, sandbox_message_id, "
                "created_at, created_by, updated_at, updated_by, published_at, published_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'imported', ?, 'imported', ?, ?)",
                (status, title, body, channel_id, message_id, sandbox_id, stamp, stamp,
                 stamp if status == "published" else None, published_by),
            )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('announcements_imported_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (stamp,),
        )
    return len(rows)

