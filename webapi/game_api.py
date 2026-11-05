"""
Dashboard API for Blizzard game data (wow_api.py).

  GET  /api/game/reference          classes/specs, races, professions, rulesets for pickers
                                    (moderators; ?fresh=1 skips the cache, admins)
  POST /api/game/explore            one raw Blizzard API request for debugging (admins):
                                    {"path": "/data/wow/ruleset/index", "namespace": "dynamic-forever-eu",
                                     "region": "eu"}

The explorer uses the app's own token only (never a member's Battle.net
login) and only reaches /data/wow/ and /profile/wow/ paths.
"""

import json

import config
import wow_api
from webapi.common import BOT_KEY, BadRequest, audit, error, forbidden_unless, json_response, read_json

VIEW_LEVEL = "moderator"
EXPLORE_LEVEL = "admin"
MAX_BODY_CHARS = 400_000  # some search documents are megabytes; the explorer is for looking, not exporting


async def reference(request):
    denied = forbidden_unless(request, VIEW_LEVEL, "game data")
    if denied:
        return denied
    fresh = request.query.get("fresh") == "1" and "admin" in request["levels"]
    try:
        data = await wow_api.reference(request.app[BOT_KEY], fresh=fresh)
    except wow_api.WowApiError as exc:
        return error(str(exc), 503)
    return json_response(data)


async def explore(request):
    denied = forbidden_unless(request, EXPLORE_LEVEL, "the Blizzard API explorer (admins only)")
    if denied:
        return denied
    try:
        body = await read_json(request)
        path, namespace, region = (str(body.get(k) or "") for k in ("path", "namespace", "region"))
        result = await wow_api.explore(request.app[BOT_KEY], path, namespace.strip(), region or None)
    except (BadRequest, wow_api.WowApiError) as exc:
        return error(str(exc), 400)
    except Exception as exc:  # network trouble: say so instead of a bare 500
        return error(f"Couldn't reach Blizzard: {type(exc).__name__}", 502)
    text = json.dumps(result["body"]) if result["body"] is not None else ""
    if len(text) > MAX_BODY_CHARS:
        result["body"] = None
        result["truncated"] = f"The answer is {len(text) / 1e6:.1f} MB; too big to show. Narrow the request."
    audit(request, "maintenance", "game.explore", f"Asked Blizzard's API for {path.split('?')[0][:120]}",
          target=namespace, details={"region": region or wow_api.region(), "status": result["status"]})
    return json_response(result)


async def explorer_defaults(request):
    denied = forbidden_unless(request, VIEW_LEVEL, "game data")
    if denied:
        return denied
    return json_response({
        "region": wow_api.region(),
        "regions": list(wow_api.REGIONS),
        "namespaces": {kind: wow_api.namespace(kind) for kind in ("static", "dynamic", "profile")},
        "game": config.GAME_VERSION,
        "configured": getattr(request.app[BOT_KEY], "blizzard", None) is not None,
        "can_explore": EXPLORE_LEVEL in request["levels"],
    })


def register(app):
    app.router.add_get("/api/game/reference", reference)
    app.router.add_get("/api/game/explorer", explorer_defaults)
    app.router.add_post("/api/game/explore", explore)
