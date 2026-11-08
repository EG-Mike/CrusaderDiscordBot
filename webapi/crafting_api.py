"""
Dashboard API for crafting (crafting.py, cogs/crafting.py).

  GET    /api/crafting                       professions with their crafters, the featured recipes with who
                                             can make them, and the channel's status
  GET    /api/crafting/professions/{id}      a profession's recipes by category, with how many crafters know
                                             each and whether it's featured
  POST   /api/crafting/featured              {"recipe_id", "profession_id"}: feature a recipe
  DELETE /api/crafting/featured/{recipe_id}  stop featuring it
  POST   /api/crafting/post                  post or update the crafting channel now

Moderators only. Featuring and removing recipes is audited, and the
crafting channel is updated right after.
"""

import asyncio
from typing import Dict, List

import crafting
import gear
import roster
import wow_api
from webapi import auth
from webapi.common import BOT_KEY, HUB_KEY, BadRequest, actor_label, audit, error, forbidden_unless, json_response, \
    read_json

LEVEL = "moderator"


def _guard(request):
    denied = forbidden_unless(request, LEVEL, "crafting")
    if denied:
        return denied, None
    cog = request.app[BOT_KEY].get_cog("CraftingCog")
    if cog is None:
        return error("Crafting is switched off (Settings, Features). It also needs the guild roster.", 409), None
    if getattr(request.app[BOT_KEY], "blizzard", None) is None:
        return error("Blizzard API keys aren't set up (Settings, General, Integrations).", 409), None
    return None, cog


def _crafter(c: dict, guild, class_names: Dict[int, str], **extra) -> dict:
    member = guild.get_member(int(c["discord_id"])) if guild is not None and c.get("discord_id") else None
    return {"slug": c["slug"], "display_name": roster.display_name(c), "class_name": class_names.get(c["class_id"]),
            "level": c["level"],
            "in_guild": bool(c["guild_slug"]), "is_main": bool(c["is_main"]),
            "member": {"id": str(member.id), "name": member.display_name, "avatar_url": member.display_avatar.url}
            if member else None, **extra}


async def _changed(request, cog):
    cog.publish_soon()
    await request.app[HUB_KEY].broadcast({"type": "crafting.changed"}, level=LEVEL)


async def overview(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    guild = auth.guild_for(bot)
    try:
        professions = await crafting.professions(bot)
        class_names = {c["id"]: c["name"] for c in (await wow_api.reference(bot)).get("classes", [])}
    except (crafting.CraftingError, wow_api.WowApiError) as exc:
        return error(str(exc), 502)
    characters = await asyncio.to_thread(crafting.crafters)
    featured = await asyncio.to_thread(crafting.featured)
    by_profession = crafting.profession_overview(characters)
    knows = crafting.who_knows(characters)
    icons: Dict[int, str] = {}
    if featured:
        icons = {k: v for k, v in (await gear.icon_urls_for(bot, [r["item_id"] for r in featured])).items() if v}
    return json_response({
        "professions": [{**p, "crafters": [_crafter(row["character"], guild, class_names, skill=row["skill"], max=row["max"])
                                           for row in by_profession.get(p["id"], [])]}
                        for p in sorted(professions, key=lambda p: p["name"])],
        "featured": [{**r, "item_url": gear.item_url(r["item_id"]),
                      "item_icon_url": icons.get(r["item_id"]) or r["icon_url"],
                      "crafters": [_crafter(c, guild, class_names,
                                            skill=crafting.profession_of(c, r["profession_id"]).get("skill"),
                                            max=crafting.profession_of(c, r["profession_id"]).get("max"))
                                   for c in crafting.by_skill(knows.get(r["recipe_id"], []), r["profession_id"])]}
                     for r in featured],
        "characters_with_recipes": sum(1 for c in characters if roster.known_recipes(c)),
        "characters": len(characters),
        "channel_problem": cog.channel_problem(),
    })


async def profession_recipes(request):
    denied, _cog = _guard(request)
    if denied:
        return denied
    value = request.match_info["id"]
    if not value.isdigit():
        return error("Unknown profession.", 404)
    try:
        catalog = await crafting.recipe_catalog(request.app[BOT_KEY], int(value))
    except crafting.CraftingError as exc:
        return error(str(exc), 502)
    knows = crafting.who_knows(await asyncio.to_thread(crafting.crafters))
    featured_ids = {r["recipe_id"] for r in await asyncio.to_thread(crafting.featured)}
    categories: List[dict] = [{"name": c["name"], "recipes": [
        {**r, "known_by": len(knows.get(r["id"], [])), "featured": r["id"] in featured_ids} for r in c["recipes"]]}
        for c in catalog["categories"]]
    return json_response({"id": catalog["id"], "name": catalog["name"], "categories": categories})


async def add_featured(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    try:
        body = await read_json(request)
        recipe_id, profession_id = body.get("recipe_id"), body.get("profession_id")
        if not isinstance(recipe_id, int) or not isinstance(profession_id, int):
            raise BadRequest("Pick a recipe from a profession's list.")
        catalog = await crafting.recipe_catalog(bot, profession_id)
        if not any(r["id"] == recipe_id for c in catalog["categories"] for r in c["recipes"]):
            raise BadRequest("That recipe isn't in this profession's list.")
        row = await crafting.add_featured(bot, recipe_id, {"id": catalog["id"], "name": catalog["name"]},
                                          by=actor_label(request))
    except (BadRequest, crafting.CraftingError) as exc:
        return error(str(exc), 400)
    audit(request, "crafting", "crafting.feature", f"Featured the recipe {row['name']} ({row['profession_name']})",
          target=str(recipe_id), details={"recipe_id": recipe_id, "profession_id": profession_id})
    await _changed(request, cog)
    return json_response(row, status=201)


async def remove_featured(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    value = request.match_info["recipe_id"]
    if not value.isdigit():
        return error("Unknown recipe.", 404)
    try:
        row = await asyncio.to_thread(crafting.remove_featured, int(value))
    except crafting.CraftingError as exc:
        return error(str(exc), 404)
    audit(request, "crafting", "crafting.unfeature", f"Stopped featuring the recipe {row['name']}",
          target=value, details={"recipe_id": int(value)})
    await _changed(request, cog)
    return json_response({"ok": True})


async def post_now(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    result = await cog.publish(force=True)
    audit(request, "crafting", "crafting.post", "Updated the crafting channel")
    return json_response({"result": result})


def register(app):
    app.router.add_get("/api/crafting", overview)
    app.router.add_get("/api/crafting/professions/{id}", profession_recipes)
    app.router.add_post("/api/crafting/featured", add_featured)
    app.router.add_delete("/api/crafting/featured/{recipe_id}", remove_featured)
    app.router.add_post("/api/crafting/post", post_now)
