"""
Links into the web dashboard, for messages the bot posts in Discord (e.g.
notifications, application cards). The dashboard is reached at
WEB_BASE_URL (an SSH tunnel by default), so these links work for the people
who have that tunnel open.
"""

import os
from typing import Optional

import config


def dashboard_link(path: str) -> Optional[str]:
    """The dashboard address of `path` (e.g. "/applications/123"), or None
    when the dashboard is off."""
    if not config.FEATURE_WEB_DASHBOARD_ENABLED:
        return None
    base = os.environ.get("WEB_BASE_URL", "http://localhost:8081").rstrip("/")
    return f"{base}{path}"
