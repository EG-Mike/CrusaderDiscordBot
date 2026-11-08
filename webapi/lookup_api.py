"""
Dashboard API for the character lookup: a character check started by
moderators, for any character in the region (gear.py), with where the
character stands on the guild roster. WarcraftLogs parses join here once
WarcraftLogs covers the game version.

  GET /api/lookup?name=First Surname    the character's summary (moderators)

Read-only: nothing is stored, so lookups aren't audited.
"""

import asyncio

import card_stats
import config
import gear
import roster
import wow_api
from webapi import auth
from webapi.common import BOT_KEY, error, forbidden_unless, json_response

LEVEL = "moderator"


def _roster_entry(guild, character_slug: str):
    c = roster.get(character_slug)
    if c is None:
        return None
    member = guild.get_member(int(c["discord_id"])) if guild and c["discord_id"] else None
    return {
        "on_roster": True,
        "in_guild": bool(c["guild_slug"]),
        "is_main": bool(c["is_main"]),
        "member": {"id": str(member.id), "name": member.display_name, "avatar_url": member.display_avatar.url}
        if member else None,
    }


async def lookup(request):
    denied = forbidden_unless(request, LEVEL, "character lookups")
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    name = request.query.get("name", "").strip()
    character_slug = wow_api.slug(name)
    if not character_slug:
        return error("Type a character's name and surname, e.g. Eggmike Thewhite.", 400)
    if getattr(bot, "blizzard", None) is None:
        return error("Blizzard API keys aren't set up (Settings, General, Integrations).", 409)
    try:
        ref = await wow_api.reference(bot)
        summary = await gear.fetch(bot, character_slug, {c["id"]: c["name"] for c in ref.get("classes", [])})
    except gear.GearError as exc:
        return error(f"Couldn't find {name} on Blizzard: {exc}", 404)
    except wow_api.WowApiError as exc:
        return error(str(exc), 502)
    icons = await gear.item_icon_urls(bot, summary)
    entry = await asyncio.to_thread(_roster_entry, auth.guild_for(bot), summary["slug"])
    return json_response({
        "character": {**summary, "items": [
            {**i, "icon_url": icons.get(i["item_id"]), "url": gear.item_url(i["item_id"])} for i in summary["items"]]},
        "off_ruleset": gear.off_ruleset(summary),
        "requirement_problems": gear.requirement_problems(summary),
        "stat_groups": gear.stat_groups(summary.get("stats") or {}),
        "statistics": {"title": config.APPLICATION_STATISTICS_TITLE or None,
                       "groups": card_stats.lines(summary.get("achievement_stats"))},
        "roster": entry,
        "warcraftlogs": None,  # once WarcraftLogs covers WoW Forever
    })


def register(app):
    app.router.add_get("/api/lookup", lookup)
