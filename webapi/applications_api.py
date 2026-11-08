"""
Dashboard API for applications (cogs/apply.py, applications.py).

  GET  /api/applications                      every application (list rows), counts, what the user may do
  GET  /api/applications/{id}                 one application: gear, statistics, note, screenshots, decision
  POST /api/applications/{id}/approve         approve (approvers)
  POST /api/applications/{id}/deny            {"reason": optional, reviewers only} (approvers)
  POST /api/applications/{id}/reset           back to pending (approvers)
  POST /api/applications/{id}/refresh-gear    read the character from Blizzard again (moderators)
  POST /api/applications/archive              move decided cards to the archive channel (moderators)

Moderators see applications (as they can in the review channel); only
approvers decide, the same rule as the card's buttons. Decisions run the
cog's own methods, so roles, nickname, DMs, the roster link and the card in
Discord all follow. The audit trail records who decided what, never the
applicant's note or the reviewer's reason.
"""

import asyncio

import applications
import gear
from applications import ApplicationError, Reviewer
from webapi import auth
from webapi.common import BOT_KEY, HUB_KEY, BadRequest, audit, error, forbidden_unless, json_response, read_json

LEVEL = "moderator"
DECIDE_LEVEL = "approver"
WHAT = "applications"


def _cog(request):
    return request.app[BOT_KEY].get_cog("ApplyCog")


def _guard(request, level: str = LEVEL):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied is None and level == DECIDE_LEVEL and DECIDE_LEVEL not in request["levels"]:
        denied = error("Only approvers can approve, deny or reset applications (Settings, Applications, "
                       "Approver role).", 403)
    if denied:
        return denied, None
    cog = _cog(request)
    if cog is None:
        return error("Applications aren't running right now. Check the System log.", 409), None
    return None, cog


def _reviewer(request) -> Reviewer:
    user = request["user"]
    return Reviewer(id=int(user["user_id"]), name=user["username"], via="dashboard")


def _row(request, message_id, record):
    bot = request.app[BOT_KEY]
    return applications.public_row(bot.store, auth.guild_for(bot), message_id, record)


async def _detail(request, cog, message_id: int, record: dict) -> dict:
    bot = request.app[BOT_KEY]
    icons = {}
    if record.get("gear") and getattr(bot, "blizzard", None) is not None:
        icons = await gear.item_icon_urls(bot, record["gear"])
    screenshots = await cog.screenshot_urls(record)
    return applications.public_detail(bot.store, auth.guild_for(bot), message_id, record, icon_urls=icons,
                                      screenshot_urls=screenshots)


async def list_applications(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    cog = _cog(request)
    rows = [_row(request, mid, rec) for mid, rec in applications.records(bot.store)]
    counts = {status: sum(1 for r in rows if r["status"] == status) for status in applications.STATUSES}
    return json_response({
        "applications": rows,
        "counts": counts,
        "running": cog is not None,
        "can_decide": DECIDE_LEVEL in request["levels"],
        "archive_problem": cog.archive_problem() if cog else None,
    })


async def get_application(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    try:
        message_id, record = applications.find(request.app[BOT_KEY].store, request.match_info["id"])
    except ApplicationError as exc:
        return error(str(exc), 404)
    return json_response(await _detail(request, cog, message_id, record))


ACTIONS = {
    # name: (level, audit word)
    "approve": (DECIDE_LEVEL, "Approved"),
    "deny": (DECIDE_LEVEL, "Denied"),
    "reset": (DECIDE_LEVEL, "Reset"),
    "refresh-gear": (LEVEL, "Refreshed the gear of"),
}


async def application_action(request):
    action = request.match_info["action"]
    if action not in ACTIONS:
        return error("Unknown action.", 404)
    level, word = ACTIONS[action]
    denied, cog = _guard(request, level)
    if denied:
        return denied
    store = request.app[BOT_KEY].store
    try:
        message_id, _record = applications.find(store, request.match_info["id"])
    except ApplicationError as exc:
        return error(str(exc), 404)
    try:
        body = await read_json(request) if request.can_read_body else {}
        if action == "approve":
            record = await cog.approve(message_id, _reviewer(request))
        elif action == "deny":
            reason = body.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise BadRequest("The reason must be text.")
            record = await cog.deny(message_id, _reviewer(request), reason)
        elif action == "reset":
            record = await cog.reset(message_id, _reviewer(request))
        else:
            record = await cog.refresh_gear(message_id)
    except (ApplicationError, BadRequest) as exc:
        return error(str(exc), 400)
    audit(request, "application", f"applications.{action.replace('-', '_')}",
          f"{word} {record['character_name']}'s application", target=str(message_id),
          details={"application_id": str(message_id), "character": record["character_name"],
                   "applicant_id": str(record["applicant_id"])})
    return json_response(await _detail(request, cog, message_id, record))


async def archive(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    user = request["user"]
    try:
        moved, failed = await cog.archive_decided(f"{user['username']} (dashboard)")
    except ApplicationError as exc:
        return error(str(exc), 400)
    audit(request, "application", "applications.archive", f"Archived {moved} decided application(s)",
          details={"moved": moved, "failed": failed})
    return json_response({"moved": moved, "failed": failed})


def register(app):
    hub = app[HUB_KEY]

    def changed(application_id: str):
        # Called from the bot's own code (Discord buttons, the apply flow) and from these handlers.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(hub.broadcast({"type": "applications.changed", "id": application_id}, level=LEVEL))

    applications.on_change("dashboard", changed)
    app.router.add_get("/api/applications", list_applications)
    app.router.add_post("/api/applications/archive", archive)
    app.router.add_get("/api/applications/{id}", get_application)
    app.router.add_post("/api/applications/{id}/{action}", application_action)
