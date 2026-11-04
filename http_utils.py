"""Small HTTP helpers shared by the API clients."""

import base64


def basic_auth_header(username: str, password: str) -> str:
    """Value for an `Authorization` header using HTTP Basic auth, as the
    OAuth client-credentials flows (WarcraftLogs, Blizzard, Discord) expect.
    Done by hand rather than with aiohttp.BasicAuth, which is deprecated and
    whose replacement only exists in recent aiohttp versions."""
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"
