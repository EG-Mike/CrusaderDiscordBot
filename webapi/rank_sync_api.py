"""
Dashboard API for rank sync (rank_sync.py, cogs/rank_sync.py).

  GET    /api/rank-sync                  the rules, the guild's ranks (names, how many members, examples),
                                         every member with a rule's role and their group, the channel's status
  POST   /api/rank-sync/rules            {"role_id", "rank"}: members with the role should have this rank
  DELETE /api/rank-sync/rules/{role_id}  remove a rule
  PUT    /api/rank-sync/ranks            {"names": {"0": "Guild Master", ...}}: the ranks' names
  POST   /api/rank-sync/post             post or update the rank sync channel now

Moderators only; changes are audited and the channel is updated right
after. Discord IDs travel as strings.
"""

import asyncio
import json

import config
import db
import rank_sync
import roster
import wow_api
from cogs.rank_sync import POST_META
from webapi import auth
from webapi.common import BOT_KEY, HUB_KEY, BadRequest, actor_label, audit, error, forbidden_unless, json_response, \
    read_json

LEVEL = "moderator"


def _guard(request):
    denied = forbidden_unless(request, LEVEL, "rank sync")
    if denied:
        return denied, None
    cog = request.app[BOT_KEY].get_cog("RankSyncCog")
    if cog is None:
        return error("Rank sync is switched off (Settings, Features). It also needs the guild roster.", 409), None
    return None, cog


def _post_status(bot, cog):
    channel = bot.get_channel(int(config.RANK_SYNC_CHANNEL_ID or 0))
    if channel is None:
        return None
    state = json.loads(db.get_meta(POST_META) or "{}")
    posted = state.get("channel_id") == channel.id and state.get("message_ids")
    return {"channel_id": str(channel.id), "channel_name": channel.name,
            "updated": state.get("updated") if posted else None, "problem": cog.channel_problem()}


def _character(c, names):
    if c is None:
        return None
    return {"slug": c["slug"], "display_name": roster.display_name(c), "class_id": c["class_id"],
            "level": c["level"], "in_guild": c["guild_slug"] == rank_sync.guild_slug(),
            "guild_rank": c["guild_rank"], "rank_label": rank_sync.rank_label(c["guild_rank"], names)
            if c["guild_slug"] == rank_sync.guild_slug() else None}


async def overview(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    guild = auth.guild_for(bot)
    if rank_sync.guild_slug() is None:
        return json_response({"guild_name": None})
    names = await asyncio.to_thread(rank_sync.rank_names)
    rule_list = await asyncio.to_thread(rank_sync.rules)
    characters = await asyncio.to_thread(roster.characters)
    entries = rank_sync.check(rank_sync.discord_members(guild, rule_list), characters, rule_list)
    role_name = {r.id: r.name for r in getattr(guild, "roles", [])}
    seen = {r["rank"]: r for r in rank_sync.ranks_seen(characters)}
    ranks = [{"rank": rank, "name": names.get(rank), "count": seen.get(rank, {}).get("count", 0),
              "examples": seen.get(rank, {}).get("examples", [])}
             for rank in sorted(set(seen) | set(names) | {r["rank"] for r in rule_list})]
    try:
        classes = (await wow_api.reference(bot)).get("classes", [])
    except wow_api.WowApiError:
        classes = []
    return json_response({
        "guild_name": config.GUILD_ROSTER_GUILD,
        "rules": [{"role_id": str(r["role_id"]), "role_name": role_name.get(r["role_id"]), "rank": r["rank"],
                   "rank_label": rank_sync.rank_label(r["rank"], names)} for r in rule_list],
        "ranks": ranks,
        "max_rank": rank_sync.MAX_RANK,
        "entries": [{"member": {"id": str(e["id"]), "name": e["name"], "avatar_url": e["avatar_url"]},
                     "role_name": role_name.get(e["role_id"]), "target_rank": e["target_rank"],
                     "target_label": rank_sync.rank_label(e["target_rank"], names), "status": e["status"],
                     "character": _character(e["character"], names)} for e in entries],
        "suggested_role_id": str(config.FRESH_ROLE_ID) if config.FRESH_ROLE_ID else None,
        "classes": classes,
        "post": _post_status(bot, cog),
    })


async def _changed(request, cog):
    cog.publish_soon()
    await request.app[HUB_KEY].broadcast({"type": "rank_sync.changed"}, level=LEVEL)


async def add_rule(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    guild = auth.guild_for(request.app[BOT_KEY])
    try:
        body = await read_json(request)
        try:
            role_id = int(body.get("role_id"))
        except (TypeError, ValueError):
            raise BadRequest("Pick a role.") from None
        role = guild.get_role(role_id) if guild is not None else None
        if role is None:
            raise BadRequest("That role isn't in the server.")
        rank = body.get("rank")
        await asyncio.to_thread(rank_sync.set_rule, role_id, rank, by=actor_label(request))
        label = rank_sync.rank_label(rank)
    except (BadRequest, rank_sync.RankSyncError) as exc:
        return error(str(exc), 400)
    audit(request, "rank_sync", "rank_sync.rule", f"Members with @{role.name} should have the rank {label}",
          target=str(role_id), details={"role_id": str(role_id), "rank": rank})
    await _changed(request, cog)
    return json_response({"ok": True})


async def remove_rule(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    try:
        role_id = int(request.match_info["role_id"])
        await asyncio.to_thread(rank_sync.remove_rule, role_id)
    except ValueError:
        return error("Unknown role.", 400)
    except rank_sync.RankSyncError as exc:
        return error(str(exc), 404)
    audit(request, "rank_sync", "rank_sync.rule_removed", "Removed a rank sync rule", target=str(role_id),
          details={"role_id": str(role_id)})
    await _changed(request, cog)
    return json_response({"ok": True})


async def set_ranks(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    try:
        body = await read_json(request)
        names = body.get("names")
        if not isinstance(names, dict) or not all(isinstance(v, str) for v in names.values()):
            raise BadRequest("Send the rank names as text.")
        try:
            typed = {int(k): v for k, v in names.items()}
        except ValueError:
            raise BadRequest("Unknown rank.") from None
        await asyncio.to_thread(rank_sync.set_rank_names, typed)
    except (BadRequest, rank_sync.RankSyncError) as exc:
        return error(str(exc), 400)
    audit(request, "rank_sync", "rank_sync.ranks", "Named the guild's ranks",
          details={"names": {str(k): v for k, v in sorted(typed.items()) if v.strip()}})
    await _changed(request, cog)
    return json_response({"ok": True})


async def post_now(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    result = await cog.publish(force=True)
    audit(request, "rank_sync", "rank_sync.post", "Updated the rank sync channel")
    return json_response({"result": result})


def register(app):
    app.router.add_get("/api/rank-sync", overview)
    app.router.add_post("/api/rank-sync/rules", add_rule)
    app.router.add_delete("/api/rank-sync/rules/{role_id}", remove_rule)
    app.router.add_put("/api/rank-sync/ranks", set_ranks)
    app.router.add_post("/api/rank-sync/post", post_now)
