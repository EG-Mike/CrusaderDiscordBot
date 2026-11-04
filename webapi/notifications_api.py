"""
Dashboard API for notifications (cogs/notifications.py).

  GET  /api/notifications        where notifications go, and any problem with it
  POST /api/notifications/test   post a test notification

Moderator access.
"""

import config
from webapi.common import BOT_KEY, actor_label, audit, error, forbidden_unless, json_response

LEVEL = "moderator"
WHAT = "notifications"


def _cog(request):
    return request.app[BOT_KEY].get_cog("NotificationsCog")


async def status(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    cog = _cog(request)
    if cog is None:
        return json_response({"enabled": False, "pending_restart": bool(config.FEATURE_NOTIFICATIONS_ENABLED)})
    return json_response({"enabled": True, **cog.destination()})


async def send_test(request):
    denied = forbidden_unless(request, LEVEL, WHAT)
    if denied:
        return denied
    cog = _cog(request)
    if cog is None:
        return error("Notifications are switched off (Settings, Features).", 409)
    from cogs.notifications import NotificationError

    try:
        channel = await cog.send_test(request["user"]["username"])
    except NotificationError as exc:
        return error(str(exc), 400)
    audit(request, "other", "notifications.test", f"Sent a test notification to #{channel}")
    return json_response({"channel": channel})


def register(app):
    app.router.add_get("/api/notifications", status)
    app.router.add_post("/api/notifications/test", send_test)
