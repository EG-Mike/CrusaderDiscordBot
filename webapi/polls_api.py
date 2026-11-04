"""
Dashboard API for polls (cogs/polls.py).

  GET    /api/polls                 every poll, plus the defaults for new ones
  GET    /api/polls/{id}            one poll
  POST   /api/polls                 post a new poll in a channel
  POST   /api/polls/{id}/action     {"action": ...}: open_voting, close, reopen_adding, reopen_voting,
                                    clear_deadline, add_option {text}, remove_options {ids}, toggle {key, value}
  DELETE /api/polls/{id}            delete the poll and its message

Moderator access (moderators can manage every poll, as in Discord). Votes
stay as private as the poll promised: counts are shown, who voted for what
only when the poll's votes are public.
"""

import config
from cogs.polls import PollError
from webapi.common import BOT_KEY, HUB_KEY, BadRequest, audit, error, forbidden_unless, json_response, read_json

LEVEL = "moderator"
WHAT = "polls"

ACTION_WORDS = {
    "open_voting": "Opened voting on", "close": "Closed", "reopen_adding": "Reopened adding on",
    "reopen_voting": "Reopened voting on", "clear_deadline": "Removed the deadline of",
    "add_option": "Added an option to", "remove_options": "Removed options from", "toggle": "Changed a setting of",
}


def _cog(request):
    return request.app[BOT_KEY].get_cog("PollsCog")


def _guard(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied, None
    cog = _cog(request)
    if cog is None:
        return error("Polls are switched off (Settings, Features).", 409), None
    return None, cog


def _id(request) -> int:
    value = request.match_info["id"]
    if not value.isdigit():
        raise BadRequest("Unknown poll.")
    return int(value)


async def _changed(request, message_id):
    await request.app[HUB_KEY].broadcast({"type": "polls.changed", "id": str(message_id)}, level=LEVEL)


def _defaults() -> dict:
    return {
        "max_adds": config.POLL_DEFAULT_MAX_ADDS_PER_MEMBER, "collect_first": config.POLL_DEFAULT_COLLECT_FIRST,
        "max_votes": config.POLL_DEFAULT_MAX_VOTES_PER_MEMBER, "live_results": config.POLL_DEFAULT_LIVE_RESULTS,
        "show_authors": config.POLL_DEFAULT_SHOW_AUTHORS, "show_voters": config.POLL_DEFAULT_SHOW_VOTERS,
        "allow_vote_change": config.POLL_DEFAULT_ALLOW_VOTE_CHANGE,
    }


async def list_polls(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    cog = _cog(request)
    return json_response({"polls": cog.dashboard_list() if cog else [], "defaults": _defaults(),
                          "feature_enabled": cog is not None})


async def get_poll(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    try:
        return json_response(cog.dashboard_get(_id(request)))
    except (PollError, BadRequest) as exc:
        return error(str(exc), 404)


async def create_poll(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    user = request["user"]
    try:
        body = await read_json(request)
        options = body.get("options") or []
        if not isinstance(options, list):
            raise BadRequest("Options must be a list.")
        settings = body.get("settings") or {}
        if not isinstance(settings, dict):
            raise BadRequest("Settings must be an object.")
        poll = await cog.dashboard_create(body.get("channel_id"), str(body.get("question") or ""),
                                          body.get("description"), [str(o) for o in options], settings,
                                          int(user["user_id"]), user["username"])
    except (PollError, BadRequest, ValueError) as exc:
        return error(str(exc) or "Invalid poll", 400)
    audit(request, "poll", "polls.create", f"Started the poll “{poll['question']}” in #{poll['channel_name']}",
          target=poll["message_id"], details={"channel_id": poll["channel_id"], "message_id": poll["message_id"]})
    await _changed(request, poll["message_id"])
    return json_response(poll, status=201)


async def poll_action(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    user = request["user"]
    try:
        message_id = _id(request)
        body = await read_json(request)
        action = body.get("action")
        if action not in ACTION_WORDS:
            raise BadRequest("Unknown action.")
        poll = await cog.dashboard_action(message_id, action, body, int(user["user_id"]), user["username"])
    except (PollError, BadRequest) as exc:
        return error(str(exc), 400)
    audit(request, "poll", f"polls.{action}", f"{ACTION_WORDS[action]} the poll “{poll['question']}”",
          target=poll["message_id"])
    await _changed(request, message_id)
    return json_response(poll)


async def delete_poll(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    try:
        message_id = _id(request)
        question = cog.dashboard_get(message_id)["question"]
        await cog.dashboard_delete(message_id)
    except (PollError, BadRequest) as exc:
        return error(str(exc), 400)
    audit(request, "poll", "polls.delete", f"Deleted the poll “{question}”", target=str(message_id))
    await _changed(request, message_id)
    return json_response({"ok": True})


def register(app):
    app.router.add_get("/api/polls", list_polls)
    app.router.add_post("/api/polls", create_poll)
    app.router.add_get("/api/polls/{id}", get_poll)
    app.router.add_post("/api/polls/{id}/action", poll_action)
    app.router.add_delete("/api/polls/{id}", delete_poll)
