"""
Dashboard API for the bot's standard messages (bot_messages.py).

  GET    /api/bot-messages                       every message: text in use, default, placeholders
  PUT    /api/bot-messages/{key}                 save (?update_discord=1 also edits the pinned message)
  DELETE /api/bot-messages/{key}                 back to the default text (same option)
  POST   /api/bot-messages/{key}/update-discord  edit/post the pinned message now
  GET    /api/bot-messages/{key}/history         saved versions, newest first

Moderator access. Audited without the text itself.
"""

import config
import bot_messages
from webapi.common import BadRequest, actor_label, audit, error, forbidden_unless, json_response, read_json

LEVEL = "moderator"
WHAT = "the bot's messages"


def _describe(d: bot_messages.Definition) -> dict:
    current = bot_messages.current(d.key)
    sample = bot_messages.sample_values(d.key)
    return {
        "key": d.key,
        "label": d.label,
        "group": d.group,
        "description": d.description,
        "kind": d.kind,
        "where": d.where,
        "title": current["title"],
        "body": current["body"],
        "color": current["color"],
        "customized": current["customized"],
        "updated_at": current["updated_at"],
        "updated_by": current["updated_by"],
        "default": {"title": d.default_title, "body": d.default_body, "color": d.default_color},
        "default_color": bot_messages.DEFAULT_COLOR,
        "footer": d.marker,  # pinned explainers show this footer (it's how the bot finds them)
        "variables": [
            {"name": v.name, "description": v.description, "value": sample.get(v.name, ""),
             "from_settings": v.source is not None}
            for v in d.variables
        ],
        "can_update_discord": d.kind == "embed" and bot_messages.can_update(d.key),
        "feature_enabled": bool(getattr(config, d.feature, True)) if d.feature else True,
        "limit": bot_messages.MAX_EMBED_TEXT if d.kind == "embed" else bot_messages.MAX_MESSAGE_TEXT,
    }


def _key(request) -> str:
    key = request.match_info["key"]
    bot_messages.definition(key)  # raises BotMessageError for unknown keys
    return key


async def _after_change(request, key: str) -> dict:
    """Optionally pushes an explainer to Discord; failures become a note,
    since the text itself was saved."""
    out = {"message": _describe(bot_messages.definition(key))}
    if request.query.get("update_discord") in ("1", "true") and bot_messages.definition(key).kind == "embed":
        try:
            out["discord"] = await bot_messages.update_in_discord(key)
            audit(request, "bot_message", "bot_messages.update_discord",
                  f"Updated the {bot_messages.definition(key).label.lower()} in Discord", target=key)
        except bot_messages.BotMessageError as exc:
            out["discord_error"] = str(exc)
    return out


async def list_messages(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    return json_response([_describe(d) for d in bot_messages.DEFINITIONS])


async def save_message(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    try:
        key = _key(request)
        body = await read_json(request)
        result = bot_messages.save(key, body.get("title"), body.get("body"), body.get("color"), actor_label(request))
    except bot_messages.BotMessageError as exc:
        return error(str(exc), 404 if str(exc) == "Unknown message." else 400)
    except BadRequest as exc:
        return error(str(exc), 400)
    label = bot_messages.definition(key).label
    audit(request, "bot_message", "bot_messages.save", f"Edited the bot message “{label}”", target=key)
    out = await _after_change(request, key)
    out["unknown_placeholders"] = bot_messages.unknown_placeholders(key, result["title"], result["body"])
    return json_response(out)


async def reset_message(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    try:
        key = _key(request)
    except bot_messages.BotMessageError as exc:
        return error(str(exc), 404)
    bot_messages.reset(key, actor_label(request))
    label = bot_messages.definition(key).label
    audit(request, "bot_message", "bot_messages.reset", f"Reset the bot message “{label}” to its default", target=key)
    return json_response(await _after_change(request, key))


async def update_discord(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    try:
        key = _key(request)
        outcome = await bot_messages.update_in_discord(key)
    except bot_messages.BotMessageError as exc:
        return error(str(exc), 409)
    audit(request, "bot_message", "bot_messages.update_discord",
          f"Updated the {bot_messages.definition(key).label.lower()} in Discord", target=key)
    return json_response({"discord": outcome})


async def message_history(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    try:
        return json_response(bot_messages.history(_key(request)))
    except bot_messages.BotMessageError as exc:
        return error(str(exc), 404)


def register(app):
    app.router.add_get("/api/bot-messages", list_messages)
    app.router.add_put("/api/bot-messages/{key}", save_message)
    app.router.add_delete("/api/bot-messages/{key}", reset_message)
    app.router.add_post("/api/bot-messages/{key}/update-discord", update_discord)
    app.router.add_get("/api/bot-messages/{key}/history", message_history)
