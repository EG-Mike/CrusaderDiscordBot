"""
Dashboard API for the announcement builder (announcements.py).

  GET    /api/announcements                    list (?status=draft|published, ?q=)
  POST   /api/announcements                    new draft (optionally from a template)
  GET    /api/announcements/{id}               one announcement + its version history
  PUT    /api/announcements/{id}               save; a published one is edited in Discord too
  POST   /api/announcements/{id}/publish       post it to a channel
  POST   /api/announcements/{id}/duplicate     copy into a new draft
  DELETE /api/announcements/{id}               delete (and its Discord messages)
  GET/POST /api/announcement-templates, PUT/DELETE /api/announcement-templates/{id}

Moderator access, like /announce. Saves carry the `revision` the editor
loaded, so two people editing the same announcement get a clear conflict
instead of overwriting each other. Draft saves (autosave) aren't audited;
creating, publishing, editing published messages, deleting and template
changes are - with the title, never the text.
"""

import asyncio

import announcements as service
import config
from webapi.common import (
    BOT_KEY, HUB_KEY, BadRequest, actor_label, audit, error, forbidden_unless, json_response, read_json,
)

LEVEL = "moderator"
WHAT = "announcements"


def _title(record) -> str:
    return f"“{record.get('title') or 'Untitled'}”"


def _public(record: dict, bot) -> dict:
    channel = bot.get_channel(int(record["channel_id"])) if record.get("channel_id") else None
    return {**record, "channel_name": getattr(channel, "name", None)}


def _guard(request, *, write: bool = False):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    if write and not config.FEATURE_ANNOUNCEMENTS_ENABLED:
        return error("Announcements are switched off. Turn them on in Settings, Features, and restart the bot.", 409)
    return None


def _id(request) -> int:
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise service.NotFound("This announcement doesn't exist.") from None


def _service_error(exc: Exception):
    if isinstance(exc, service.Conflict):
        return json_response({"error": str(exc), "current": exc.current}, status=409)
    if isinstance(exc, service.NotFound):
        return error(str(exc), 404)
    return error(str(exc), 400)


async def _changed(request, announcement_id, deleted: bool = False):
    await request.app[HUB_KEY].broadcast(
        {"type": "announcements.changed", "id": announcement_id, "deleted": deleted}, level=LEVEL,
    )


# --- announcements -------------------------------------------------------------------------

async def list_announcements(request):
    denied = _guard(request)
    if denied:
        return denied
    status = request.query.get("status") or None
    if status and status not in service.STATUSES:
        return error("Unknown status", 400)
    q = (request.query.get("q") or "").strip()[:100] or None
    records, counts = await asyncio.gather(
        asyncio.to_thread(service.list_announcements, status, q), asyncio.to_thread(service.counts),
    )
    bot = request.app[BOT_KEY]
    return json_response({
        "announcements": [_public(r, bot) for r in records],
        "counts": counts,
        "feature_enabled": bool(config.FEATURE_ANNOUNCEMENTS_ENABLED),
    })


async def get_announcement(request):
    denied = _guard(request)
    if denied:
        return denied
    try:
        record = service.get(_id(request))
    except service.AnnouncementError as exc:
        return _service_error(exc)
    return json_response({"announcement": _public(record, request.app[BOT_KEY]), "versions": service.versions(record["id"])})


async def create_announcement(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    try:
        body = await read_json(request)
        fields = {k: body.get(k) for k in service.EDITABLE_FIELDS if k in body}
        if body.get("template_id") is not None:
            template = service.get_template(body["template_id"])
            fields = {**{k: template[k] for k in service.EDITABLE_FIELDS}, **fields}
        record = service.create(fields, actor_label(request))
    except (BadRequest, service.AnnouncementError) as exc:
        return _service_error(exc)
    audit(request, "announcement", "announcements.create", f"Started a draft announcement {_title(record)}",
          target=str(record["id"]))
    await _changed(request, record["id"])
    return json_response(_public(record, request.app[BOT_KEY]), status=201)


async def save_announcement(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    try:
        announcement_id = _id(request)
        body = await read_json(request)
        fields = {k: body.get(k) for k in service.EDITABLE_FIELDS if k in body}
        revision = body.get("revision")
        current = service.get(announcement_id)
        if current["status"] == "published":
            record = await service.edit_published(bot, announcement_id, fields, actor_label(request), revision=revision)
            audit(request, "announcement", "announcements.edit_published",
                  f"Edited the published announcement {_title(record)}", target=str(record["id"]),
                  details={"channel_id": record["channel_id"], "message_id": record["message_id"]})
        else:
            record = service.update(announcement_id, fields, actor_label(request), revision=revision)
            await service.refresh_sandbox(bot, record)
    except (BadRequest, service.AnnouncementError) as exc:
        return _service_error(exc)
    await _changed(request, record["id"])
    return json_response(_public(record, request.app[BOT_KEY]))


async def publish_announcement(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    try:
        announcement_id = _id(request)
        body = await read_json(request)
        record = await service.publish(request.app[BOT_KEY], announcement_id, body.get("channel_id"),
                                       actor_label(request), revision=body.get("revision"))
    except (BadRequest, service.AnnouncementError) as exc:
        return _service_error(exc)
    audit(request, "announcement", "announcements.publish", f"Published the announcement {_title(record)}",
          target=str(record["id"]),
          details={"channel_id": record["channel_id"], "message_id": record["message_id"],
                   "ping": record.get("ping")})
    await _changed(request, record["id"])
    return json_response(_public(record, request.app[BOT_KEY]))


async def duplicate_announcement(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    try:
        source = service.get(_id(request))
        record = service.create({k: source[k] for k in service.EDITABLE_FIELDS}, actor_label(request))
    except service.AnnouncementError as exc:
        return _service_error(exc)
    audit(request, "announcement", "announcements.create", f"Started a draft announcement {_title(record)} "
          f"(copy of #{source['id']})", target=str(record["id"]))
    await _changed(request, record["id"])
    return json_response(_public(record, request.app[BOT_KEY]), status=201)


async def delete_announcement(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    try:
        record = service.get(_id(request))
        notes = await service.delete_everywhere(request.app[BOT_KEY], record["id"])
    except service.AnnouncementError as exc:
        return _service_error(exc)
    what = "published announcement" if record["status"] == "published" else "draft announcement"
    audit(request, "announcement", "announcements.delete", f"Deleted the {what} {_title(record)}",
          target=str(record["id"]), details={"channel_id": record.get("channel_id"), "message_id": record.get("message_id")})
    await _changed(request, record["id"], deleted=True)
    return json_response({"ok": True, "notes": notes})


# --- templates ----------------------------------------------------------------------------

async def list_templates(request):
    denied = _guard(request)
    if denied:
        return denied
    return json_response(service.list_templates())


async def create_template(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    try:
        body = await read_json(request)
        template = service.create_template(body.get("name"), body, actor_label(request))
    except (BadRequest, service.AnnouncementError) as exc:
        return _service_error(exc)
    audit(request, "announcement", "templates.create", f"Saved the announcement template “{template['name']}”",
          target=str(template["id"]))
    return json_response(template, status=201)


async def update_template(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    try:
        body = await read_json(request)
        template = service.update_template(_id(request), body.get("name"), body, actor_label(request))
    except (BadRequest, service.AnnouncementError) as exc:
        return _service_error(exc)
    audit(request, "announcement", "templates.update", f"Changed the announcement template “{template['name']}”",
          target=str(template["id"]))
    return json_response(template)


async def delete_template(request):
    denied = _guard(request, write=True)
    if denied:
        return denied
    try:
        template = service.get_template(_id(request))
    except service.AnnouncementError as exc:
        return _service_error(exc)
    service.delete_template(template["id"])
    audit(request, "announcement", "templates.delete", f"Deleted the announcement template “{template['name']}”",
          target=str(template["id"]))
    return json_response({"ok": True})


def register(app):
    app.router.add_get("/api/announcements", list_announcements)
    app.router.add_post("/api/announcements", create_announcement)
    app.router.add_get("/api/announcements/{id}", get_announcement)
    app.router.add_put("/api/announcements/{id}", save_announcement)
    app.router.add_post("/api/announcements/{id}/publish", publish_announcement)
    app.router.add_post("/api/announcements/{id}/duplicate", duplicate_announcement)
    app.router.add_delete("/api/announcements/{id}", delete_announcement)
    app.router.add_get("/api/announcement-templates", list_templates)
    app.router.add_post("/api/announcement-templates", create_template)
    app.router.add_put("/api/announcement-templates/{id}", update_template)
    app.router.add_delete("/api/announcement-templates/{id}", delete_template)
