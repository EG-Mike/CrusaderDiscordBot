"""
Who may call which dashboard API route, plus the request checks a
dashboard reachable from the internet needs: the visitor's real address
behind a reverse proxy, a limit on login attempts, and an Origin check on
requests that change something.

Access levels (webapi/auth.levels_for works out a member's levels):

  admin      settings, API keys, setup guide, restart, maintenance tools,
             console output, the bot's profile
  moderator  the guild management pages (announcements, polls, roster,
             crafting, rank sync, auction prices, audit log, ...)
  reviewer   the Applications and Character lookup pages
  viewer     read-only general pages (Overview, roster, leveling,
             crafting), only while viewer access is switched on

Higher levels include the lower ones (an admin is a moderator, a
moderator is a reviewer and a viewer). ANY means "has any level", i.e.
may use the dashboard at all.

ROUTES is the single list of every /api route and the level it needs.
server.require_login refuses a route that isn't in it (deny by default),
and tests/test_access.py fails when a route is added without an entry.
The feature modules' own checks (common.forbidden_unless) stay as a
second line of defence.
"""

import ipaddress
import os
import time
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

ANY = "any"
ADMIN, MODERATOR, REVIEWER, VIEWER = "admin", "moderator", "reviewer", "viewer"
LEVELS = (ADMIN, MODERATOR, REVIEWER, VIEWER)

# (method, route path as registered) -> level needed.
ROUTES: Dict[Tuple[str, str], str] = {
    # The dashboard itself
    ("GET", "/api/me"): ANY,
    ("GET", "/api/status"): ANY,  # trimmed for non-moderators (server._status)
    ("GET", "/api/ws"): ANY,  # events are limited per level (common.Hub)
    ("*", "/api/{tail}"): ANY,  # "Not found" for unknown API paths
    ("GET", "/api/guild/channels"): MODERATOR,
    ("GET", "/api/guild/roles"): MODERATOR,

    # Admin: settings, API keys, setup, restart, maintenance tools
    ("GET", "/api/settings"): ADMIN,
    ("PUT", "/api/settings/{key}"): ADMIN,
    ("DELETE", "/api/settings/{key}"): ADMIN,
    ("GET", "/api/settings/{key}/history"): ADMIN,
    ("GET", "/api/integrations"): ADMIN,
    ("PUT", "/api/integrations/{key}"): ADMIN,
    ("DELETE", "/api/integrations/{key}"): ADMIN,
    ("POST", "/api/integrations/{service}/test"): ADMIN,
    ("GET", "/api/setup"): ADMIN,
    ("POST", "/api/setup/complete"): ADMIN,
    ("POST", "/api/bot/restart"): ADMIN,
    ("PUT", "/api/branding/logo"): ADMIN,
    ("DELETE", "/api/branding/logo"): ADMIN,
    ("GET", "/api/bot/profile"): ADMIN,
    ("PUT", "/api/bot/profile"): ADMIN,
    ("GET", "/api/maintenance"): MODERATOR,  # the tools themselves are admin-only
    ("GET", "/api/maintenance/backup"): ADMIN,
    ("POST", "/api/maintenance/{action}"): ADMIN,
    ("GET", "/api/game/reference"): MODERATOR,
    ("GET", "/api/game/explorer"): MODERATOR,
    ("POST", "/api/game/explore"): ADMIN,

    # Logs
    ("GET", "/api/audit"): MODERATOR,
    ("GET", "/api/audit/meta"): MODERATOR,
    ("GET", "/api/audit/export"): MODERATOR,
    ("GET", "/api/system-log"): MODERATOR,  # console output inside it: admins only
    ("GET", "/api/system-log/meta"): MODERATOR,

    # Communication
    ("GET", "/api/announcements"): MODERATOR,
    ("POST", "/api/announcements"): MODERATOR,
    ("GET", "/api/announcements/{id}"): MODERATOR,
    ("PUT", "/api/announcements/{id}"): MODERATOR,
    ("POST", "/api/announcements/{id}/publish"): MODERATOR,
    ("POST", "/api/announcements/{id}/duplicate"): MODERATOR,
    ("DELETE", "/api/announcements/{id}"): MODERATOR,
    ("GET", "/api/announcement-templates"): MODERATOR,
    ("POST", "/api/announcement-templates"): MODERATOR,
    ("PUT", "/api/announcement-templates/{id}"): MODERATOR,
    ("DELETE", "/api/announcement-templates/{id}"): MODERATOR,
    ("GET", "/api/bot-messages"): MODERATOR,
    ("PUT", "/api/bot-messages/{key}"): MODERATOR,
    ("DELETE", "/api/bot-messages/{key}"): MODERATOR,
    ("POST", "/api/bot-messages/{key}/update-discord"): MODERATOR,
    ("GET", "/api/bot-messages/{key}/history"): MODERATOR,
    ("GET", "/api/emoji"): MODERATOR,
    ("GET", "/api/emoji/search"): MODERATOR,
    ("POST", "/api/emoji"): MODERATOR,
    ("DELETE", "/api/emoji/{id}"): MODERATOR,
    ("GET", "/api/polls"): MODERATOR,
    ("POST", "/api/polls"): MODERATOR,
    ("GET", "/api/polls/{id}"): MODERATOR,
    ("POST", "/api/polls/{id}/action"): MODERATOR,
    ("DELETE", "/api/polls/{id}"): MODERATOR,
    ("GET", "/api/notifications"): MODERATOR,
    ("POST", "/api/notifications/test"): MODERATOR,

    # Members
    ("GET", "/api/roster"): VIEWER,
    ("GET", "/api/roster/members"): MODERATOR,  # Discord members, for linking
    ("POST", "/api/roster/sync"): MODERATOR,
    ("POST", "/api/roster/post"): MODERATOR,
    ("POST", "/api/roster/characters"): MODERATOR,
    ("POST", "/api/roster/characters/{slug}"): MODERATOR,
    ("GET", "/api/leveling"): VIEWER,
    ("GET", "/api/lookup"): REVIEWER,
    ("GET", "/api/crafting"): VIEWER,
    ("GET", "/api/crafting/professions/{id}"): VIEWER,
    ("POST", "/api/crafting/featured"): MODERATOR,
    ("DELETE", "/api/crafting/featured/{recipe_id}"): MODERATOR,
    ("POST", "/api/crafting/order"): MODERATOR,
    ("POST", "/api/crafting/post"): MODERATOR,
    ("POST", "/api/crafting/lockpickers"): MODERATOR,
    ("DELETE", "/api/crafting/lockpickers/{slug}"): MODERATOR,
    ("GET", "/api/auction"): MODERATOR,
    ("GET", "/api/auction/search"): MODERATOR,
    ("GET", "/api/auction/houses"): MODERATOR,
    ("POST", "/api/auction/house"): MODERATOR,
    ("POST", "/api/auction/compare"): MODERATOR,
    ("POST", "/api/auction/refresh"): MODERATOR,
    ("POST", "/api/auction/try"): MODERATOR,
    ("GET", "/api/rank-sync"): MODERATOR,
    ("POST", "/api/rank-sync/rules"): MODERATOR,
    ("DELETE", "/api/rank-sync/rules/{role_id}"): MODERATOR,
    ("PUT", "/api/rank-sync/ranks"): MODERATOR,
    ("POST", "/api/rank-sync/post"): MODERATOR,

    # Applications
    ("GET", "/api/applications"): REVIEWER,
    ("GET", "/api/applications/{id}"): REVIEWER,
    ("POST", "/api/applications/{id}/{action}"): REVIEWER,  # approve, deny, reset, refresh
    ("POST", "/api/applications/archive"): MODERATOR,
    ("GET", "/api/applications/statistics"): MODERATOR,
    ("PUT", "/api/applications/statistics"): MODERATOR,
    ("POST", "/api/applications/statistics/scan"): MODERATOR,
}


def required_level(method: str, path: str) -> Optional[str]:
    """The level a route needs, or None for a route that isn't listed."""
    method = "GET" if method == "HEAD" else method  # aiohttp adds HEAD for every GET
    return ROUTES.get((method, path))


def allows(levels: Iterable[str], required: str) -> bool:
    """Whether `levels` include `required` (counting what they imply)."""
    levels = implied(levels)
    if required == ANY:
        return bool(levels & set(LEVELS))
    return required in levels


def implied(levels: Iterable[str]) -> set:
    """Adds the levels a level includes: admin -> moderator -> reviewer and
    viewer. (Viewer roles only count while viewer access is on; that's
    decided where the roles are read, in auth.levels_for.)"""
    levels = set(levels)
    if ADMIN in levels:
        levels.add(MODERATOR)
    if MODERATOR in levels:
        levels.update((REVIEWER, VIEWER))
    return levels


# --- the visitor's address behind a reverse proxy ---------------------------------

# Addresses whose X-Forwarded-For is believed: the reverse proxy (Caddy).
# The default covers the server itself and Docker's private networks, which
# is where a proxy on the same server connects from; the dashboard's port
# is only published on 127.0.0.1, so nothing else can reach it.
DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"


def _networks(raw: str) -> list:
    networks = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                networks.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return networks


_TRUSTED = _networks(os.environ.get("WEB_TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES))


def _is_trusted(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in network for network in _TRUSTED)


def client_ip(request) -> str:
    """The visitor's address: the connecting peer, or - when that's a
    trusted proxy - the last address in X-Forwarded-For that isn't one."""
    peer = request.remote or ""
    if not _is_trusted(peer):
        return peer
    hops = [hop.strip() for hop in request.headers.get("X-Forwarded-For", "").split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop):
            return hop
    return hops[0] if hops else peer


# --- login attempt limit ---------------------------------------------------------------

class RateLimiter:
    """At most `limit` hits per `seconds` for each key (an address). Kept in
    memory: a restart forgets it, which is fine for slowing down abuse."""

    def __init__(self, limit: int, seconds: float):
        self.limit = limit
        self.seconds = seconds
        self._hits: Dict[str, list] = {}

    def hit(self, key: str, now: Optional[float] = None) -> bool:
        """Counts a hit; False when the key is over its limit."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.seconds
        if len(self._hits) > 10000:  # many addresses: drop the ones that went quiet
            self._hits = {k: [t for t in v if t > cutoff] for k, v in self._hits.items()}
            self._hits = {k: v for k, v in self._hits.items() if v}
        hits = [t for t in self._hits.get(key, []) if t > cutoff]
        allowed = len(hits) < self.limit
        if allowed:
            hits.append(now)
        self._hits[key] = hits
        return allowed


# A login is two requests (/auth/login, then Discord sends you back to
# /auth/callback), so this allows about 15 logins per 10 minutes per address.
LOGIN_LIMIT = RateLimiter(limit=30, seconds=600)


# --- Origin check -----------------------------------------------------------------------

def origin_allowed(request, base_url: str) -> bool:
    """Requests that change something (and the WebSocket) must come from
    the dashboard's own pages. Browsers always send Origin on those; a
    request without one isn't from another site's page (a browser would
    have sent it), and the custom request header is still required."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    parts = urlsplit(origin)
    if not parts.scheme or not parts.netloc:
        return False  # "null" (sandboxed frames, file://) and anything malformed
    base = urlsplit(base_url)
    if (parts.scheme, parts.netloc) == (base.scheme, base.netloc):
        return True
    # The same host the browser asked for (e.g. the frontend's dev server,
    # or an SSH tunnel's localhost address).
    return parts.netloc == request.host
