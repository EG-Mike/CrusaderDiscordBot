"""
Dashboard API for emoji (emoji_library.py): the editor's emoji picker and
the Emoji page.

  GET    /api/emoji             the bot's emoji (library + others), the server's emoji
  GET    /api/emoji/search?q=   items/spells by name, or pasted links/IDs, with icons
  POST   /api/emoji             add items/spells: {"items": [{"kind", "game_id"}]}
  DELETE /api/emoji/{id}        delete a library emoji (from Discord too)

Moderator access. Adding and deleting need the Emoji admin feature on.
"""

import asyncio

import aiohttp
import discord

import config
import emoji_library
from webapi import auth
from webapi.common import BOT_KEY, HUB_KEY, BadRequest, actor_label, audit, error, forbidden_unless, json_response, read_json

LEVEL = "moderator"
WHAT = "emoji"
MAX_PER_REQUEST = 15


def _feature_off():
    if not config.FEATURE_EMOJI_ADMIN_ENABLED:
        return error("Adding and deleting emoji is switched off (Settings, Features, Emoji admin).", 409)
    return None


def _server_emoji(bot) -> list:
    guild = auth.guild_for(bot)
    if guild is None:
        return []
    return [{"emoji_id": str(e.id), "name": e.name, "animated": e.animated, "available": e.available,
             "url": emoji_library.emoji_image_url(e.id, e.animated), "text": str(e)}
            for e in sorted(guild.emojis, key=lambda e: e.name.lower())]


async def list_emoji(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    try:
        overview = await emoji_library.overview(bot)
    except (discord.HTTPException, aiohttp.ClientError):
        return error("Couldn't load the bot's emoji from Discord. Is the bot connected?", 503)
    return json_response({**overview, "server": _server_emoji(bot),
                          "can_manage": bool(config.FEATURE_EMOJI_ADMIN_ENABLED),
                          "blizzard_configured": getattr(bot, "blizzard", None) is not None})


async def search(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    query = (request.query.get("q") or "").strip()[:2000]
    try:
        results = await emoji_library.search(request.app[BOT_KEY], query)
    except emoji_library.EmojiError as exc:
        return error(str(exc), 400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return error("Blizzard's API didn't answer. Try again in a moment.", 502)
    return json_response(results)


async def add_emoji(request):
    denied = forbidden_unless(request, LEVEL, WHAT) or _feature_off()
    if denied:
        return denied
    try:
        body = await read_json(request)
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise BadRequest("Choose at least one item or spell.")
        if len(items) > MAX_PER_REQUEST:
            raise BadRequest(f"Add at most {MAX_PER_REQUEST} at a time.")
        refs = []
        for item in items:
            kind, game_id = item.get("kind"), item.get("game_id")
            if kind not in emoji_library.KINDS or not str(game_id).isdigit():
                raise BadRequest("Each entry needs a kind (item or spell) and a numeric game_id.")
            refs.append((kind, int(game_id)))
    except BadRequest as exc:
        return error(str(exc), 400)

    bot, by = request.app[BOT_KEY], actor_label(request)
    results = []
    for kind, game_id in refs:
        try:
            result = await emoji_library.add(bot, kind, game_id, by)
        except emoji_library.EmojiError as exc:
            results.append({"kind": kind, "game_id": game_id, "status": "error", "error": str(exc)})
            continue
        results.append(result)
        if result["status"] != "exists":
            name = result["item"].get("name") or f"{kind} {game_id}"
            what = "Added the emoji" if result["status"] == "created" else "Reused the emoji"
            audit(request, "emoji", f"emoji.{result['status']}", f"{what} :{result['emoji']['name']}: for {name}",
                  target=result["emoji"]["emoji_id"], details={"kind": kind, "game_id": game_id})
    await request.app[HUB_KEY].broadcast({"type": "emoji.changed"}, level=LEVEL)
    return json_response({"results": results})


async def delete_emoji(request):
    denied = forbidden_unless(request, LEVEL, WHAT) or _feature_off()
    if denied:
        return denied
    try:
        entry = await emoji_library.delete(request.app[BOT_KEY], int(request.match_info["id"]))
    except ValueError:
        return error("Unknown emoji", 404)
    except emoji_library.EmojiError as exc:
        return error(str(exc), 400)
    audit(request, "emoji", "emoji.delete", f"Deleted the emoji :{entry['emoji_name']}:", target=entry["emoji_id"])
    await request.app[HUB_KEY].broadcast({"type": "emoji.changed"}, level=LEVEL)
    return json_response({"ok": True})


def register(app):
    app.router.add_get("/api/emoji", list_emoji)
    app.router.add_get("/api/emoji/search", search)
    app.router.add_post("/api/emoji", add_emoji)
    app.router.add_delete("/api/emoji/{id}", delete_emoji)
