"""
Dashboard API for the guild roster and leveling (roster.py, cogs/roster.py,
cogs/leveling.py).

  GET  /api/roster                    every character, the class/spec names and how the last sync went
  GET  /api/roster/members?q=         server members matching a name, for the link picker
  POST /api/roster/sync               read the guild roster and check profiles now (and update the posted roster)
  POST /api/roster/post               post or update the roster in the roster channel now
  POST /api/roster/characters         {"name": "First Surname"}: find a character on Blizzard and add it
  POST /api/roster/characters/{slug}  {"action": "link", "discord_id"} | {"action": "unlink"} | {"action": "main"}
                                      | {"action": "specs", "main_spec_id", "off_spec_id"}
  GET  /api/leveling                  the leaderboard, recent level-ups and the milestone settings

Moderators only (as in Discord, moderators can link any character to
anyone). Every change is audited.
"""

import asyncio
import json

import config
import db
import roster
import wow_api
from cogs.roster import POST_META, last_sync, reference
from webapi import auth
from webapi.common import (
    BOT_KEY, HUB_KEY, BadRequest, actor_label, audit, error, forbidden_unless, json_response, read_json,
)

LEVEL = "moderator"
MAX_MEMBER_RESULTS = 20


def _guard(request):
    denied = forbidden_unless(request, LEVEL, "the guild roster")
    if denied:
        return denied
    if request.app[BOT_KEY].get_cog("RosterCog") is None:
        return error("The guild roster is switched off (Settings, Features).", 409)
    return None


def _member(guild, discord_id):
    member = guild.get_member(int(discord_id)) if guild and discord_id else None
    if member is None:
        return None
    return {"id": str(member.id), "name": member.display_name, "avatar_url": member.display_avatar.url}


def _serialize(c: dict, guild) -> dict:
    return {
        "slug": c["slug"], "name": c["name"], "surname": c["surname"], "display_name": roster.display_name(c),
        "class_id": c["class_id"], "race_id": c["race_id"], "faction": c["faction"], "level": c["level"],
        "ruleset_id": c["ruleset_id"], "guild_slug": c["guild_slug"], "guild_rank": c["guild_rank"],
        "discord_id": str(c["discord_id"]) if c["discord_id"] else None,
        "member": _member(guild, c["discord_id"]) if c["discord_id"] else None,
        "is_main": bool(c["is_main"]), "main_spec_id": c["main_spec_id"], "off_spec_id": c["off_spec_id"],
        "last_login": c["last_login"], "profile_found": c["profile_found"], "updated_at": c["updated_at"],
    }


async def _changed(request):
    await request.app[HUB_KEY].broadcast({"type": "roster.changed"}, level=LEVEL)


async def overview(request):
    denied = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    guild = auth.guild_for(bot)
    rows = await asyncio.to_thread(roster.characters)
    ref = await reference(bot)
    return json_response({
        "guild_name": config.GUILD_ROSTER_GUILD or None,
        "self_service": bool(config.GUILD_ROSTER_SELF_SERVICE),
        "last_sync": last_sync(),
        "characters": [_serialize(c, guild) for c in rows],
        "classes": ref.get("classes", []),
        "rulesets": ref.get("rulesets", []),
        "post": _post_status(bot),
    })


def _post_status(bot):
    channel = bot.get_channel(int(config.GUILD_ROSTER_CHANNEL_ID or 0))
    if channel is None:
        return None
    state = json.loads(db.get_meta(POST_META) or "{}")
    posted = state.get("channel_id") == channel.id and state.get("message_ids")
    return {"channel_id": str(channel.id), "channel_name": channel.name,
            "updated": state.get("updated") if posted else None}


async def post(request):
    denied = _guard(request)
    if denied:
        return denied
    result = await request.app[BOT_KEY].get_cog("RosterCog").publish_roster(force=True)
    if result is None:
        return error("Pick a roster channel first (Settings, Guild roster).", 400)
    if not result.startswith("Posted"):
        return error(result, 400)
    audit(request, "roster", "roster.post", "Posted the guild roster in Discord")
    return json_response({"result": result, "post": _post_status(request.app[BOT_KEY])})


async def members(request):
    denied = forbidden_unless(request, LEVEL, "the guild roster")
    if denied:
        return denied
    guild = auth.guild_for(request.app[BOT_KEY])
    needle = request.query.get("q", "").strip().lower()
    found = []
    for member in (guild.members if guild else []):
        if member.bot:
            continue
        if needle and needle not in member.display_name.lower() and needle not in member.name.lower():
            continue
        found.append(_member(guild, member.id))
        if len(found) == MAX_MEMBER_RESULTS:
            break
    return json_response({"members": sorted(found, key=lambda m: m["name"].lower())})


async def sync(request):
    denied = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    result = await bot.get_cog("RosterCog").sync()
    leveling = bot.get_cog("LevelingCog")
    checked = await leveling.check_now() if leveling else None
    await bot.get_cog("RosterCog").publish_roster()
    status = last_sync() or {}
    audit(request, "roster", "roster.sync", "Synced the guild roster with Blizzard",
          details={"ok": status.get("ok"), "members": status.get("members")})
    await _changed(request)
    return json_response({
        "last_sync": status,
        "members": result["members"] if result else None,
        "checked": checked["checked"] if checked else None,
        "level_ups": len((result or {}).get("events", [])) + len((checked or {}).get("events", [])),
        "announced": checked["announced"] if checked else 0,
    })


async def add_character(request):
    denied = _guard(request)
    if denied:
        return denied
    try:
        body = await read_json(request)
        c = await roster.find_or_add(request.app[BOT_KEY], str(body.get("name") or ""))
    except (BadRequest, roster.RosterError, wow_api.WowApiError) as exc:
        return error(str(exc), 400)
    audit(request, "roster", "roster.add", f"Added {roster.display_name(c)} to the roster", target=c["slug"])
    await _changed(request)
    return json_response(_serialize(c, auth.guild_for(request.app[BOT_KEY])))


ACTIONS = {"link": "Linked", "unlink": "Unlinked", "main": "Made the main:", "specs": "Set the specs of"}


async def character_action(request):
    denied = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    character_slug = request.match_info["slug"]
    try:
        body = await read_json(request)
        action = body.get("action")
        current = await asyncio.to_thread(roster.get, character_slug)
        if current is None:
            raise roster.RosterError("That character isn't on the roster.")
        owner = current["discord_id"] or 0
        if action == "link":
            target = str(body.get("discord_id") or "")
            guild = auth.guild_for(bot)
            if not target.isdigit() or (guild and guild.get_member(int(target)) is None):
                raise BadRequest("Pick a member of the server.")
            c = await asyncio.to_thread(roster.link, character_slug, int(target), by=actor_label(request),
                                        as_moderator=True)
        elif action == "unlink":
            c = await asyncio.to_thread(roster.unlink, character_slug, discord_id=owner, as_moderator=True)
        elif action == "main":
            c = await asyncio.to_thread(roster.set_main, character_slug, discord_id=owner, as_moderator=True)
        elif action == "specs":
            ref = await reference(bot)
            class_specs = {k["id"]: [s["id"] for s in k["specs"]] for k in ref.get("classes", [])}

            def spec(value):
                if value in (None, ""):
                    return None
                if not str(value).isdigit():
                    raise BadRequest("Pick a spec from the list.")
                return int(value)

            c = await asyncio.to_thread(roster.set_specs, character_slug, spec(body.get("main_spec_id")),
                                        spec(body.get("off_spec_id")), discord_id=owner, as_moderator=True,
                                        class_specs=class_specs)
        else:
            raise BadRequest("Unknown action.")
    except (BadRequest, roster.RosterError) as exc:
        return error(str(exc), 400)
    details = {"discord_id": str(c["discord_id"])} if action == "link" else None
    audit(request, "roster", f"roster.{action}", f"{ACTIONS[action]} {roster.display_name(c)}", target=c["slug"],
          details=details)
    await _changed(request)
    return json_response(_serialize(c, auth.guild_for(bot)))


async def leveling_overview(request):
    denied = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    guild = auth.guild_for(bot)
    channel = bot.get_channel(int(config.LEVELING_CHANNEL_ID or 0))
    rows = await asyncio.to_thread(roster.leaderboard, 100)
    events = await asyncio.to_thread(roster.recent_events, 40)
    return json_response({
        "enabled": bot.get_cog("LevelingCog") is not None,
        "channel": {"id": str(channel.id), "name": channel.name} if channel else None,
        "milestones": config.LEVELING_MILESTONES,
        "announce_alts": bool(config.LEVELING_ANNOUNCE_ALTS),
        "refresh_minutes": config.LEVELING_REFRESH_MINUTES,
        "leaderboard": [_serialize(c, guild) for c in rows],
        "events": [{
            "id": e["id"], "created_at": e["created_at"], "old_level": e["old_value"], "new_level": e["new_value"],
            "milestone": e["milestone"], "outcome": e["outcome"], "announced_at": e["announced_at"],
            "slug": e["slug"], "display_name": roster.display_name(e) if e["name"] else "Unknown character",
            "class_id": e["class_id"], "member": _member(guild, e["discord_id"]) if e["discord_id"] else None,
        } for e in events],
    })


def register(app):
    app.router.add_get("/api/roster", overview)
    app.router.add_get("/api/roster/members", members)
    app.router.add_post("/api/roster/sync", sync)
    app.router.add_post("/api/roster/post", post)
    app.router.add_post("/api/roster/characters", add_character)
    app.router.add_post("/api/roster/characters/{slug}", character_action)
    app.router.add_get("/api/leveling", leveling_overview)
