"""
Pieces every dashboard API module shares: app keys, JSON responses, the
live-update hub, access checks and dashboard audit records. Feature API
modules (e.g. announcements_api.py) import from here, and server.py wires
them into the app.
"""

import json
from typing import Optional

from aiohttp import web

import audit as audit_store

CSRF_HEADER = "X-Crusader-Request"

BOT_KEY = web.AppKey("bot", object)
HUB_KEY = web.AppKey("hub", object)
STARTED_KEY = web.AppKey("started", float)


def json_response(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, default=str))


def error(message: str, status: int) -> web.Response:
    return json_response({"error": message}, status=status)


class BadRequest(Exception):
    """Raised by request helpers; turned into a 400 with its message."""


async def read_json(request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BadRequest("The request body must be JSON") from None
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object")
    return body


def forbidden_unless(request, level: str, what: str) -> Optional[web.Response]:
    """A 403 response if the user lacks `level`, else None."""
    if level not in request["levels"]:
        return error(f"You don't have access to {what}", 403)
    return None


class Hub:
    """Fan-out of events to every open dashboard tab. Each socket remembers
    who opened it, so events can be limited to an access level and sockets
    are closed once their session ends or their roles no longer allow
    access (see server._revalidate_sockets)."""

    def __init__(self):
        self.sockets = {}  # ws -> {"user_id", "token", "levels"}

    def add(self, ws, user_id: int, token: str, levels: set):
        self.sockets[ws] = {"user_id": user_id, "token": token, "levels": set(levels)}

    def discard(self, ws):
        self.sockets.pop(ws, None)

    async def broadcast(self, event: dict, level: str = "moderator"):
        payload = json.dumps(event, default=str)
        for ws, info in list(self.sockets.items()):
            if level not in info["levels"]:
                continue
            try:
                await ws.send_str(payload)
            except Exception:
                self.discard(ws)


def audit(request, kind: str, action: str, summary: str, *, target=None, details=None):
    """Records a dashboard action in the audit trail (audit.py). Always on
    while the dashboard runs. Never pass secrets or private text."""
    user = request["user"]
    audit_store.record(
        source="dashboard", kind=kind, action=action, summary=summary,
        actor_id=user["user_id"], actor_name=user["username"], target=target, details=details,
    )


def actor_label(request) -> str:
    """'name (id)' - how dashboard changes are attributed in stored records."""
    user = request["user"]
    return f"{user['username']} ({user['user_id']})"
